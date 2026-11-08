This is the final round. There is no further exchange with the external
reviewer, so the output is a plan, not another analysis.

Below is the reviewer's response to your rebuttal. Re-read the relevant
code before responding.

PART 1 — Resolution. Keep this tight; most items are settled.

  CLOSED — the reviewer withdrew. One line, no re-litigating.
  CONCEDED — you're changing position. Name the specific new evidence or
    code path that changed it. If you can't name one, you aren't
    conceding, you're capitulating; use MAINTAINED instead.
  MAINTAINED — you still disagree. This is a legitimate outcome and I'd
    rather have a real disagreement than a manufactured consensus. State
    what would change your mind, and what breaks if you're wrong.
  EMPIRICAL — give the exact command or test that settles it, plus what
    you expect the result to be, so I can interpret it in one glance.

  Where the reviewer reranked your options or proposed a different fix,
  respond on the remedy separately from the finding. Accepting that a bug
  is real doesn't oblige you to accept their preferred fix.

PART 2 — The plan. Organize by action, not by finding.

  IMPLEMENT NOW — ordered. For each: what changes, rough blast radius
    (files/functions touched), and whether it's independent or depends on
    another item. Group anything touching the same code.
  DEFER — real, but not this changeset. Say why, and what would promote
    it to now.
  DROP — say why in one line.

PART 3 — Decisions I need to make. Cap this at seven. Everything else
should arrive with a default I can accept by not responding.

  Include only items where the choice turns on something you can't
  resolve: confirmed coin flips, calls that depend on product priorities
  or timeline, and anything where you and the reviewer still disagree on
  a high-severity finding. For each, describe the decision fully enough
  that someone who hasn't followed this review could propose solutions:
  what's being decided, the finding or plan item it comes from, the code
  and behavior it affects, the constraints or facts that bear on it, and
  why it's still open (a coin flip, a product or timeline call, or an
  unresolved disagreement). Don't list options, solutions or a
  preference, since the reviewer will be asked for its own, and don't
  let one in as a hint: frame what's being decided as an open question
  rather than "whether to do X", give facts about the code and the
  situation rather than about one approach, and leave out what any
  approach would cost or cause.

Finally: list any position you changed across all three rounds and what
caused it. I'm using that to judge whether this process is working.

Don't make any changes until I say so.

Reviewer's response:
