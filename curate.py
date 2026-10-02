"""
curate.py

The story-picking agent. Instead of grabbing the first post that fits a length
window (old reddit_fetch.py behavior), this:

  1. Pulls a batch of candidate posts from the subreddit
  2. Fetches the top comments for each candidate (audience reaction signal)
  3. Asks Claude to score every candidate on short-form-video virality --
     the model judges the STORY ITSELF first (hook, emotional intensity,
     narratability) and uses the comments as corroborating evidence of how
     a real audience reacted
  4. Saves the winner as narration-ready text (same story.txt / .meta.json
     contract as reddit_fetch.py, so narrate.py and build_video.py are
     untouched)

Needs ANTHROPIC_API_KEY in the environment. One Claude call per run (all
candidates scored in a single request), so cost is a fraction of a cent and
it runs fine from a Raspberry Pi.

    python3 curate.py tifu --listing top --time-filter day --out story.txt
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode emoji in story
# titles/captions -- a bare print() would crash the whole run.
# (Streams are None under pythonw, e.g. when daily.py imports the pool.)
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        _stream.reconfigure(encoding="utf-8", errors="replace")

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

import prefetch
from reddit_fetch import clean_text, fetch_comments, fetch_listing, using_oauth

load_dotenv(Path(__file__).resolve().parent / ".env")

# In RSS mode every comment fetch costs ~1 minute of rate-limit pacing, so we
# only deep-judge the top slice of the (already popularity-ranked) listing.
# (prefetch.py crawls ahead in the background to hide that wait.)
RSS_CANDIDATE_CAP = 8

# Rotation pool for `subreddit = auto`. Repeats = weighting. The vibe:
# unhinged confessions, moral-outrage AITA chaos, creepy horror, and
# "professionals reveal the case that broke them" question threads.
SUBREDDIT_POOL = [
    "TrueOffMyChest", "TrueOffMyChest",
    "confession", "confessions",
    "AmItheAsshole", "AITAH",
    "tifu",
    "nosleep", "LetsNotMeet",
    "AskReddit", "AskReddit",
]

# Question subs: the post has no body -- the STORY is the top comment
# (e.g. "doctors, what deathbed confession stuck with you?"). We promote the
# best-fitting top comment to be the narration body.
COMMENT_DRIVEN_SUBS = {"askreddit"}

# Sonnet 5: near-Opus judgment quality at a fraction of the cost -- right
# tradeoff for scoring ~25 stories twice a day.
MODEL = "claude-sonnet-5"

# The ending doctor is the one creative-writing call in the pipeline, and it
# runs once per posted story -- Opus's stronger prose is worth the pennies.
DOCTOR_MODEL = "claude-opus-4-8"

# How many comments per candidate to show the judge. Top 6 is plenty of
# signal without bloating the prompt.
COMMENTS_PER_POST = 6

SYSTEM_PROMPT = """\
You are the story curator for a short-form video channel (YouTube Shorts /
TikTok) that narrates Reddit stories over gameplay footage. The audience is
doomscrolling with a ~2-second patience fuse. Your job is to find the story
that STOPS THE SCROLL -- the freaky, unhinged, out-of-pocket, "no way this
happened", "I have to hear where this goes" story. Mild, wholesome, or
merely-relatable content is worthless here no matter how well written.

THE FIRST 5-10 SECONDS RULE (overriding): a viewer only ever hears the title
and the first sentence or two before deciding to swipe. If the title + first
two sentences would not make a stranger physically stop scrolling, the
overall score MUST be below 50 regardless of how good the rest is. A wild
ending cannot save a slow open.

THE LANDING RULE: a banger needs BOTH the hook AND the payoff -- but a weak
ending is FIXABLE in post-production (a script doctor punches up flat
endings before narration). So: score primarily on hook + premise + raw
material quality. If the opening is scroll-stopping but the ending deflates,
DO NOT tank the overall score -- keep it high and set needs_ending_fix=true.
Only score low when the premise itself is mundane; no ending rewrite can
save a boring setup.

How to judge -- in this priority order:

1. YOUR OWN READ OF THE STORY (primary). Judge the text itself:
   - Hook (weight this heaviest): is the title itself a jaw-dropper? Do the
     opening lines promise chaos immediately?
   - Shock/freak factor: is it genuinely unhinged, disturbing, absurd,
     scandalous, enraging, or so weird it demands to be shared? The best
     picks make someone say "what the actual f***" out loud. Funny works
     too, but it must be laugh-out-loud absurd, not chuckle-mild.
   - Arc & payoff: escalation, then a twist, punchline, or gut-punch.
   - Narratability: works READ ALOUD in 60-120 seconds, no images/links.

2. AUDIENCE REACTION (secondary, corroborating evidence). Top comments show
   how real readers reacted. Strong: shock, disbelief, "I gasped", hot
   debate, tagging friends, demanding updates. Weak: polite sympathy,
   advice-only replies, indifference. Confirm or temper your own read with
   this -- never substitute it for judging the story.

Score down hard: stories that are probably fake in a BORING way (obvious
creative-writing homework scores low; wild-but-plausible is fine -- and a
story so entertaining viewers won't care is acceptable), anything sexually
explicit or involving minors in unsafe contexts (score 0 -- unusable for
monetized platforms), and stories needing visuals to land.
"""


class CandidateScore(BaseModel):
    index: int = Field(description="Candidate number, matching the prompt")
    overall: int = Field(description="0-100 overall virality score")
    category: str = Field(description="funny | scary | infuriating | weird | heartwarming | jaw-dropping | other")
    hook: int = Field(description="0-100: does the title + opening grab within 3 seconds")
    emotional_intensity: int = Field(description="0-100: strength of the funny/scary/weird/outrage payload")
    narratability: int = Field(description="0-100: works read aloud in 60-120s, no visuals/links needed")
    audience_signal: int = Field(description="0-100: how strongly the top comments corroborate a big reaction")
    needs_ending_fix: bool = Field(description="true when the hook/premise is strong but the ending is flat and should be punched up before narration")
    reason: str = Field(description="1-2 sentences justifying the score")


class Scorecard(BaseModel):
    scores: list[CandidateScore]
    best_index: int = Field(description="index of the single best candidate")
    verdict: str = Field(description="one sentence on why the winner wins")


class UploadCopy(BaseModel):
    tiktok_caption: str = Field(description=(
        "TikTok caption: first line is a curiosity-gap reaction to the story "
        "(NEVER a summary, NEVER spoil the ending), then 4-6 hashtags mixing "
        "niche (#redditstories, subreddit-specific) and broad (#storytime, "
        "#fyp), then a short comment-bait question."
    ))
    youtube_title: str = Field(description=(
        "YouTube Shorts title: the reddit post title, lightly punched up, "
        "optionally one emoji, ending with '(Reddit Stories)'. Max ~90 chars."
    ))
    youtube_description: str = Field(description=(
        "1-2 line YouTube description ending with hashtags including #shorts "
        "#reddit #redditstories"
    ))


def shortlist(posts, subreddit, limit, min_len, max_len, seen_ids):
    """The candidates a run judges (and fetches comments for). Shared with
    prefetch.py so the cache holds exactly what a run will ask for."""
    candidates = gather_candidates(posts[:limit], min_len, max_len, seen_ids,
                                   allow_empty_body=subreddit.lower() in COMMENT_DRIVEN_SUBS)
    # In RSS mode every comment fetch costs ~1 minute, so cap the slice.
    if not using_oauth():
        candidates = candidates[:RSS_CANDIDATE_CAP]
    return candidates


def gather_candidates(posts, min_len, max_len, seen_ids, allow_empty_body=False):
    candidates = []
    for post in posts:
        if post.get("stickied") or post.get("id") in seen_ids:
            continue
        body = post.get("selftext", "")
        if body in ("[removed]", "[deleted]"):
            continue
        if not allow_empty_body:
            if not body or not (min_len <= len(body) <= max_len):
                continue
        candidates.append({
            "id": post["id"],
            "title": post["title"],
            "body": clean_text(body),
            "subreddit": post["subreddit"],
            "permalink": f"https://reddit.com{post['permalink']}",
            "score": post.get("score", 0),
            "upvote_ratio": post.get("upvote_ratio", 0.0),
            "num_comments": post.get("num_comments", 0),
        })
    return candidates


def attach_comments(candidates):
    live = []
    for cand in candidates:
        cached = prefetch.cached_comments(cand["id"])
        if cached is not None:
            cand["comments"] = cached[:COMMENTS_PER_POST]
        else:
            live.append(cand)
    if len(live) < len(candidates):
        print(f"  {len(candidates) - len(live)}/{len(candidates)} served from the prefetch cache")
    if live and not using_oauth():
        print(f"  fetching {len(live)} live, ~1 min each (rate limited)")
    for i, cand in enumerate(live):
        try:
            cand["comments"] = fetch_comments(cand["permalink"], limit=COMMENTS_PER_POST)
        except Exception as e:
            print(f"  (comments failed for '{cand['title'][:50]}': {e})", file=sys.stderr)
            cand["comments"] = []
        # Be polite to reddit's public endpoints -- one request per second.
        if i < len(live) - 1:
            time.sleep(1.0)


def promote_comments(candidates, min_len, max_len):
    """For question-thread subs: the top comment IS the story. Promote the
    first (= highest-ranked) comment that fits the narration length window
    into the candidate's body; remaining comments stay as audience signal.
    Candidates with no fitting comment are dropped."""
    kept = []
    for cand in candidates:
        for k, cm in enumerate(cand["comments"]):
            if min_len <= len(cm["body"]) <= max_len:
                cand["body"] = clean_text(cm["body"])
                cand["comments"] = cand["comments"][:k] + cand["comments"][k + 1:]
                cand["from_comment"] = True
                kept.append(cand)
                break
    return kept


def build_prompt(candidates):
    blocks = []
    for i, c in enumerate(candidates):
        comments = "\n".join(
            (f'  - ({cm["score"]} points) ' if cm["score"] is not None else "  - ")
            + cm["body"][:400]
            for cm in c["comments"]
        ) or "  (no comments available)"
        if c["score"] is not None:
            meta = (f"Post score: {c['score']} | Upvote ratio: {c['upvote_ratio']} | "
                    f"Comment count: {c['num_comments']}")
        else:
            meta = (f"Vote metadata unavailable; candidates are listed in reddit's "
                    f"top-of-day order, so candidate {i} ranked #{i + 1} today")
        blocks.append(
            f"### Candidate {i}\n"
            f"Subreddit: r/{c['subreddit']} | {meta}\n"
            f"Title: {c['title']}\n"
            f"Story:\n{c['body']}\n"
            f"Top comments:\n{comments}\n"
        )
    return (
        "Score every candidate below for short-form video virality, then pick "
        "the single best one.\n\n" + "\n".join(blocks)
    )


def score_candidates(candidates) -> Scorecard:
    client = anthropic.Anthropic()
    system = SYSTEM_PROMPT
    try:
        from feedback import taste_memory
        taste = taste_memory()
        if taste:
            system = system + "\n\n" + taste
            print("(judge is using the owner taste profile from ratings.json)")
    except Exception as e:
        print(f"(taste profile unavailable: {e})", file=sys.stderr)
    try:
        from metrics import performance_memory
        perf = performance_memory()
        if perf:
            system = system + "\n\n" + perf
            print("(judge is using real platform results from metrics.json)")
    except Exception as e:
        print(f"(performance memory unavailable: {e})", file=sys.stderr)
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": build_prompt(candidates)}],
        output_format=Scorecard,
    )
    return response.parsed_output


class ScriptPolish(BaseModel):
    rewrote_ending: bool = Field(description="true if the ending was changed")
    spoken_title: str = Field(description=(
        "the title as the narrator should SAY it: abbreviations expanded "
        "(AITAH -> 'am I the asshole', TIFU -> 'today I messed up'), times/"
        "numbers in spoken form, platform-safe vocabulary. Same meaning and "
        "roughly same length as the original title."
    ))
    story: str = Field(description="the full polished story body, ending punched up if it was flat")


def script_doctor(winner: dict, flagged: bool) -> ScriptPolish:
    """One Opus pass that turns the raw reddit text into a narration script:
    Black Mirror ending if the original fizzles, broken English smoothed,
    abbreviations expanded for speech, TikTok-safe vocabulary."""
    client = anthropic.Anthropic()
    hint = (
        "The curator flagged this story's ending as flat -- rewrite it."
        if flagged else
        "The curator thinks the ending is fine -- only rewrite it if you strongly disagree."
    )
    taste = ""
    try:
        from feedback import taste_memory
        block = taste_memory()
        if block:
            taste = (
                "\n\n" + block +
                "\n\nApply whatever in the owner's notes concerns the WRITING "
                "(twist style, pacing, tone, how endings land); ignore notes "
                "that are purely about story selection."
            )
    except Exception:
        pass
    response = client.messages.parse(
        model=DOCTOR_MODEL,
        thinking={"type": "adaptive"},
        # Generous cap: adaptive thinking shares this budget, and twist-
        # crafting is think-heavy. Unused headroom costs nothing.
        max_tokens=12000,
        system=(
            "You are the script doctor for a shorts channel narrating Reddit "
            "stories aloud over gameplay footage. You turn raw reddit text "
            "into a NARRATION SCRIPT. Four jobs, in order of importance:\n\n"
            "1. THE ENDING must hit like a BLACK MIRROR TWIST -- an "
            "unexpected final reveal that recontextualizes everything the "
            "viewer just heard and leaves them stunned, then scrambling to "
            "the comments. The last line IS the twist, stated flatly -- no "
            "moralizing, no reflection, no wrap-up after it. NEVER end on a "
            "question to the audience ('so am I the asshole?', 'has anyone "
            "else experienced this?') -- cut such closers even if you change "
            "nothing else. The twist must feel seeded by the story; you may "
            "plant one small detail earlier so it lands. If the original "
            "ending already delivers a genuine shock-twist, keep it.\n\n"
            "2. MAKE IT SOUND SPOKEN. Smooth broken English and awkward "
            "grammar into natural, conversational American English -- keep "
            "the first-person voice, personality, and every story detail, "
            "just make it flow when read aloud. Expand anything a narrator "
            "shouldn't say as letters: 'AITAH' -> 'am I the asshole', 'TIFU' "
            "-> 'today I messed up', 'OP' -> 'the original poster', '24M' -> "
            "'a 24 year old guy'. Convert times and numbers to spoken form: "
            "'1800' -> '6pm', '$1.5k' -> 'fifteen hundred dollars'.\n\n"
            "3. PLATFORM-SAFE VOCABULARY (TikTok community guidelines). "
            "Replace flagged words with the genre's standard substitutes so "
            "the video doesn't get taken down, keeping it natural: kill/"
            "killed -> 'unalive'/'unalived', suicide -> 'unalived "
            "themselves', died -> 'passed away' or 'didn't make it', gun -> "
            "'weapon', drugs or specific drug names -> 'substances'/'stuff' "
            "(pick what reads naturally in context), sexual assault -> 'SA'. "
            "Use judgment for other risky terms; the audience knows this "
            "code and it should not feel forced.\n\n"
            "4. CONSTRAINTS: stay within +-15% of the original length; "
            "plausible enough not to read as fiction; nothing sexually "
            "explicit, nothing unsafe involving minors."
        ) + taste,
        messages=[{
            "role": "user",
            "content": f"{hint}\n\nTitle: {winner['title']}\n\nStory:\n{winner['body']}",
        }],
        output_format=ScriptPolish,
    )
    result = response.parsed_output
    result.story = result.story.strip()
    result.spoken_title = result.spoken_title.strip().rstrip(".")
    print("Script doctor: ending rewritten." if result.rewrote_ending
          else "Script doctor: original ending kept.")
    return result


def generate_upload_copy(winner: dict) -> UploadCopy:
    """One extra cheap Claude call: platform-ready caption text for the
    winning story, saved alongside it so the uploaders can post as-is."""
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=6000,  # headroom for adaptive thinking + the copy itself
        system=(
            "You write upload copy for a shorts channel that narrates wild "
            "Reddit stories over gameplay footage. Your captions create "
            "curiosity gaps that make people watch, never summaries that let "
            "them skip. Match the story's energy (funny/creepy/outrageous)."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Write the upload copy for this story from r/{winner['subreddit']}.\n\n"
                f"Title: {winner['title']}\n\nStory:\n{winner['body']}"
            ),
        }],
        output_format=UploadCopy,
    )
    return response.parsed_output


def main():
    ap = argparse.ArgumentParser(description="Pick the most viral-worthy Reddit story via Claude scoring.")
    ap.add_argument("subreddit", nargs="?", default="auto",
                    help="a subreddit name, or 'auto' to rotate through the built-in pool")
    ap.add_argument("--listing", default="top", choices=["top", "hot", "new", "controversial"])
    ap.add_argument("--time-filter", default="day", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--limit", type=int, default=25, help="how many posts to pull as candidates")
    ap.add_argument("--min-len", type=int, default=400)
    ap.add_argument("--max-len", type=int, default=2200)
    ap.add_argument("--min-score", type=int, default=65,
                    help="if the best candidate scores below this, exit 2 (skip this run rather than post a dud)")
    ap.add_argument("--max-videos", type=int, default=3,
                    help="produce up to this many stories per crawl (every candidate clearing --min-score, best first)")
    ap.add_argument("--seen-file", default="seen_story_ids.json")
    ap.add_argument("--out", default="story.txt")
    args = ap.parse_args()

    # Fail fast: on a cold prefetch cache the Claude call happens AFTER ~9 minutes of rate-limited
    # reddit crawling -- don't start that crawl if we can't score at the end.
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set in this terminal.\n"
            "Fix: open a NEW terminal (the key is saved user-level), or run:\n"
            '  $env:ANTHROPIC_API_KEY = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY","User")',
            file=sys.stderr,
        )
        sys.exit(1)

    seen_path = Path(args.seen_file)
    seen_ids = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()

    def fully_prefetched(sub):
        posts = prefetch.cached_listing(sub, args.listing, args.time_filter)
        if posts is None:
            return False
        cands = shortlist(posts, sub, args.limit, args.min_len, args.max_len, seen_ids)
        return bool(cands) and all(prefetch.cached_comments(c["id"]) is not None for c in cands)

    if args.subreddit == "auto":
        # Rotate among subreddits the prefetcher already has fully cached, so
        # the run skips the crawl; fall back to the whole pool on a cold cache.
        ready = [s for s in SUBREDDIT_POOL if fully_prefetched(s)]
        subreddit = random.choice(ready or SUBREDDIT_POOL)
    else:
        subreddit = args.subreddit
    comment_driven = subreddit.lower() in COMMENT_DRIVEN_SUBS

    print(f"Fetching candidates from r/{subreddit} ({args.listing}/{args.time_filter})"
          + (" [question thread: story = top comment]" if comment_driven else "") + "...")
    # Keep the background prefetcher off reddit while this run crawls, or the
    # two processes share one rate limit and 429 each other.
    with prefetch.paused():
        posts = prefetch.cached_listing(subreddit, args.listing, args.time_filter)
        if posts is not None:
            print("  listing served from the prefetch cache")
        else:
            posts = fetch_listing(subreddit, args.listing, args.time_filter, args.limit)
        candidates = shortlist(posts, subreddit, args.limit, args.min_len, args.max_len, seen_ids)
        if not candidates:
            print("No usable candidates (all seen, removed, or wrong length).", file=sys.stderr)
            sys.exit(1)

        print(f"Fetching top comments for {len(candidates)} candidates...")
        attach_comments(candidates)

    if comment_driven:
        candidates = promote_comments(candidates, args.min_len, args.max_len)
        if not candidates:
            print("No question thread had a comment fitting the narration length.", file=sys.stderr)
            sys.exit(1)

    print("Scoring with Claude...")
    card = score_candidates(candidates)

    for s in sorted(card.scores, key=lambda s: -s.overall):
        marker = " <== WINNER" if s.index == card.best_index else ""
        print(f"  [{s.overall:3d}] #{s.index} ({s.category}) hook={s.hook} "
              f"emotion={s.emotional_intensity} narrate={s.narratability} "
              f"audience={s.audience_signal} -- {s.reason}{marker}")
    print(f"Verdict: {card.verdict}")

    qualifying = sorted(
        (s for s in card.scores if s.overall >= args.min_score),
        key=lambda s: -s.overall,
    )[: args.max_videos]

    if not qualifying:
        best = max(card.scores, key=lambda s: s.overall)
        print(f"Best candidate only scored {best.overall} (< {args.min_score}). "
              f"Skipping this run.", file=sys.stderr)
        sys.exit(2)

    # The crawl is the expensive part -- turn every qualifying story from it
    # into a video-ready package: doctored ending + upload copy + files.
    base = Path(args.out)
    batch = []
    for rank, score in enumerate(qualifying, 1):
        winner = candidates[score.index]
        seen_ids.add(winner["id"])

        print(f"\n[{rank}/{len(qualifying)}] '{winner['title'][:70]}' ({score.overall}/100)")
        print("  Running the script doctor...")
        polish = script_doctor(winner, score.needs_ending_fix)
        winner["body"] = polish.story

        out_txt = base.with_name(f"{base.stem}-{rank}.txt")
        # The narrator reads spoken_title; the card graphic shows the
        # original title (visual authenticity vs. speakability).
        out_txt.write_text(f"{polish.spoken_title}. {winner['body']}", encoding="utf-8")
        meta = {k: v for k, v in winner.items() if k not in ("body", "comments")}
        meta["spoken_title"] = polish.spoken_title
        meta["curation"] = score.model_dump()
        meta["ending_rewritten"] = polish.rewrote_ending
        out_txt.with_suffix(".meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

        print("  Writing platform upload copy...")
        copy = generate_upload_copy(winner)
        upload = copy.model_dump()
        # Both platforms get their AI toggle set at upload time; recorded here
        # so the uploaders (and a human posting manually) don't forget.
        upload["ai_disclosure"] = {"tiktok_aigc_label": True, "youtube_altered_content": True}
        upload["source_permalink"] = winner["permalink"]
        out_txt.with_suffix(".upload.json").write_text(
            json.dumps(upload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        batch.append(str(out_txt))

    seen_path.write_text(json.dumps(sorted(seen_ids)), encoding="utf-8")
    base.with_suffix(".batch.json").write_text(json.dumps(batch, indent=2), encoding="utf-8")
    print(f"\n{len(batch)} story package(s) ready.")


if __name__ == "__main__":
    main()
