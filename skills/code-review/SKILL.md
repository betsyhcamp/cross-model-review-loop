---
description: Session B. Run the Codex review debate for a prepared review through Claude's final plan, then decision rounds and the Decided Plan when asked.
argument-hint: <review-name>
disable-model-invocation: true
allowed-tools: Bash(xreview *)
---

# Cross-model code review (Session B)

Review name: $0

This is Session B, a fresh session that runs the review debate with Codex. If no review name was passed to this command, ask the user for it and stop.

First run `xreview prompt --template shared/protocol` and follow it.

1. Start Codex's review (Step 2): run `xreview start $0` in the background.
2. While Codex works, read `.reviews/$0/claude-handoff.md` and everything it lists. Don't change anything.
3. When the call finishes, run `xreview prompt $0 --template code/step3-claude-rebuttal` and follow it. The feedback it refers to is Codex's review, in the `response.md` file whose path the runner printed. Write your rebuttal to `.reviews/$0/messages/03-rebuttal.md`.
4. Send the rebuttal (Step 4): run `xreview continue $0 --template code/step4-reviewer-response --body messages/03-rebuttal.md --sent-heading "Step 3 — Claude rebuttal — sent to Codex" --response-heading "Step 4 — Codex response"` in the background, and wait for it to finish.
5. Run `xreview prompt $0 --template code/step5-claude-plan` and follow it. The reviewer's response is Codex's Step 4 response, in the `response.md` file whose path the runner printed. Write the plan to `.reviews/$0/messages/05-plan.md`, then run `xreview append $0 --heading "Step 5 — Claude final plan" --file messages/05-plan.md`. Show the plan to the user with the transcript path, `.reviews/$0/transcript.md`, and stop.
6. Decision rounds run only when the user asks for one, for example "get Codex's take on 2 and 4". Never start one on your own.
   - Round 1:
     1. Write `.reviews/$0/messages/06-decisions-r1.md`: the open decisions the user asked about (Step 5 Part 3, as refined in conversation), then the user's question, if they asked one, word for word under "My question:".
     2. Run `xreview continue $0 --template shared/decision-ranking --template shared/reviewer-guard --body messages/06-decisions-r1.md --sent-heading "Decision round 1 — sent to Codex" --response-heading "Decision round 1 — Codex ranking"` in the background.
     3. Rank the decisions yourself, without opening anything Codex wrote for this call. Run `xreview prompt $0 --template shared/decision-ranking` and follow it for the decisions in `06-decisions-r1.md`. Write your ranking to `.reviews/$0/messages/06-claude-ranking-r1.md`, then run `xreview append $0 --heading "Decision round 1 — Claude ranking" --file messages/06-claude-ranking-r1.md`. If Codex finishes first, don't open its response until your ranking is appended.
     4. Once the call has finished and your ranking is appended, run `xreview prompt $0 --template shared/decision-compare-claude` and follow it. The reviewer's view is Codex's ranking, in the `response.md` file whose path the runner printed. Write the comparison to `.reviews/$0/messages/06-compare-r1.md`, then run `xreview append $0 --heading "Decision round 1 — Claude comparison" --file messages/06-compare-r1.md`.
     5. Show the user both rankings and the comparison, and stop.

     If the Codex call fails and the user approves a retry, keep the ranking you appended. Don't rank again.
   - Round N, for N = 2, 3, and so on, only when the user asks for another round:
     1. Write `.reviews/$0/messages/06-followup-rN.md`: your latest comparison, then the user's question, if they asked one, word for word under "My question:".
     2. Run `xreview continue $0 --template shared/decision-followup-reviewer --body messages/06-followup-rN.md --sent-heading "Decision round N — sent to Codex" --response-heading "Decision round N — Codex response"` in the background, with N replaced by the round number.
     3. When it finishes, run `xreview prompt $0 --template shared/decision-compare-claude` and follow it. The reviewer's view is Codex's response, in the `response.md` file whose path the runner printed. Write the comparison to `.reviews/$0/messages/06-compare-rN.md`, then run `xreview append $0 --heading "Decision round N — Claude comparison" --file messages/06-compare-rN.md`. Show the comparison to the user, and stop.
7. Finalize when the user gives their decisions or says to finalize:
   1. Write `.reviews/$0/decided-plan.md` in the format below. A fresh session with no history must be able to implement it from this file alone, so name the files and functions each group changes.
   2. Count the `### Group` headings. If there are more than 15, don't append yet. Tell the user: "This plan has N groups, more than 15, which usually means the change is too big for one review. Choose one: 1. Proceed with all N groups. 2. Defer lower-priority groups. 3. Split off a follow-up change." Then stop. For choice 2 or 3, move groups from Implement now to Deferred, each marked "lower priority" (choice 2) or "follow-up change" (choice 3), until 15 or fewer remain. Move the groups the user names, or your choice if they name none. Then repeat this step. For choice 1, go on to the next step.
   3. Run `xreview append $0 --heading "Decided Plan" --file decided-plan.md`.
   4. Show the user the path `.reviews/$0/decided-plan.md`, and tell them: "Run `/xreview:code-implement $0` here, or in a fresh session if this one is long."

   The format of `decided-plan.md`:

   ```markdown
   ## User decisions
   1. <decision> → <choice> — <user's words, or "default accepted">

   ## Implement now — one commit per group, in order
   ### Group 1: <title>
   - Items: <Step 5 item references>
   - Change: <what changes>; Files: <paths/functions>
   - Done when: <observable check>
   - Depends on: <group n | none>

   ## Deferred
   - <item> — <why>; promote if <condition>

   ## Dropped
   - <item> — <why>
   ```
