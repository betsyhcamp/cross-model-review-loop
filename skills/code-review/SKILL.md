---
description: Session B. Run the Codex review debate for a prepared review through Claude's final plan and a first decision round, then more rounds and the Decided Plan when asked.
argument-hint: <review-name> [--no-decision-rounds]
disable-model-invocation: true
allowed-tools: Bash(xreview *)
---

# Cross-model code review (Session B)

Review name: $0
Arguments: $ARGUMENTS

This is Session B, a fresh session that runs the review debate with Codex. If no review name was passed to this command (the arguments above are empty), ask the user for it and stop. If the review name starts with `--`, tell the user to put the review name first, and stop. The only option is `--no-decision-rounds`, after the review name. If the arguments hold anything else after the review name, tell the user that, and stop.

First run `xreview prompt --template shared/protocol` and follow it.

1. Start Codex's review (Step 2): run `xreview start $0` in the background.
2. While Codex works, read `.reviews/$0/claude-handoff.md` and everything it lists. Don't change anything.
3. When the call finishes, run `xreview prompt $0 --template code/step3-claude-rebuttal` and follow it. The feedback it refers to is Codex's review, in the `response.md` file whose path the runner printed. Write your rebuttal to `.reviews/$0/messages/03-rebuttal.md`.
4. Send the rebuttal (Step 4): run `xreview continue $0 --template code/step4-reviewer-response --body messages/03-rebuttal.md --sent-heading "Step 3 — Claude rebuttal — sent to Codex" --response-heading "Step 4 — Codex response"` in the background, and wait for it to finish.
5. Run `xreview prompt $0 --template code/step5-claude-plan` and follow it. The reviewer's response is Codex's Step 4 response, in the `response.md` file whose path the runner printed. Write the plan to `.reviews/$0/messages/05-plan.md`. If `--no-decision-rounds` was passed and Part 3 has decisions, end the plan with Part 4, under the heading "Part 4 — Options for the open decisions (Claude)": run `xreview prompt $0 --template shared/decision-ranking` and follow it for the Part 3 decisions. Then run `xreview append $0 --heading "Step 5 — Claude final plan" --file messages/05-plan.md`.
   - If `--no-decision-rounds` was passed, or Part 3 has no decisions, show the plan to the user with the transcript path, `.reviews/$0/transcript.md`, and stop.
   - Otherwise, go straight on to decision round 1 without stopping.
6. Decision rounds. Round 1 runs straight after Step 5, unless `--no-decision-rounds` was passed or Part 3 has no decisions. Every later round runs only when the user asks for one, for example "get Codex's take on 2 and 4" or "ask Codex about 3: …". Never start one on your own. A question for you alone needs no round: answer it in conversation, and if your recommendation on a decision changes, say so plainly. Number the rounds 1, 2, 3 and so on, in the order they run. N is this round's number: put it in place of N in every file name, heading and command in this step. Part 4 holds the options for the open decisions; for each decision, the latest Part 4 that covers it counts.
   - A ranking round is for decisions no round has ranked yet. Round 1 is always one, and covers every Part 3 decision.
     1. Write `.reviews/$0/messages/06-decisions-rN.md`: the descriptions of these decisions, copied word for word from Step 5 Part 3, and nothing else from the plan, such as Part 4, defaults or preferences, so that Codex ranks without seeing your view. For a decision the user asked about that Part 3 doesn't have, such as a default they want decided, first write a description in Part 3's style: enough for someone who hasn't followed this review to propose solutions, with no options, solutions or preference. Then add the user's question, if they asked one, word for word under "My question:".
     2. Run `xreview continue $0 --template shared/decision-ranking --template shared/reviewer-guard --body messages/06-decisions-rN.md --sent-heading "Decision round N — sent to Codex" --response-heading "Decision round N — Codex ranking"` in the background.
     3. If the Step 5 plan's Part 4 already ranks some of these decisions, that's your ranking for them; rank only the rest. Rank them without opening anything Codex wrote for this call: run `xreview prompt $0 --template shared/decision-ranking` and follow it for those decisions in `06-decisions-rN.md`. Write your ranking to `.reviews/$0/messages/06-claude-ranking-rN.md`, then run `xreview append $0 --heading "Decision round N — Claude ranking" --file messages/06-claude-ranking-rN.md`. If Codex finishes first, don't open its response until your ranking is appended.
     4. Once the call has finished and you have your ranking of every decision in this round, run `xreview prompt $0 --template shared/decision-merge-claude` and follow it. The reviewer's view is Codex's ranking, in the `response.md` file whose path the runner printed. Write Part 4 to `.reviews/$0/messages/06-merge-rN.md`, then run `xreview append $0 --heading "Decision round N — Claude merged options" --file messages/06-merge-rN.md`.
     5. Show the user this Part 4, after the Step 5 plan if they haven't seen the plan yet, with the transcript path, `.reviews/$0/transcript.md`. Then stop.

     If your ranking was appended before a retry, don't rank again.
   - A follow-up round is for decisions already merged, when the user asks for another round on them or for Codex's input on one of them:
     1. Write `.reviews/$0/messages/06-followup-rN.md`: the latest Part 4 for these decisions, then the user's question, if they asked one, word for word under "My question:".
     2. Run `xreview continue $0 --template shared/decision-followup-reviewer --body messages/06-followup-rN.md --sent-heading "Decision round N — sent to Codex" --response-heading "Decision round N — Codex response"` in the background.
     3. When it finishes, run `xreview prompt $0 --template shared/decision-merge-claude` and follow it; your own view is the latest Part 4 for these decisions. The reviewer's view is Codex's response, in the `response.md` file whose path the runner printed. Write Part 4 to `.reviews/$0/messages/06-merge-rN.md`, then run `xreview append $0 --heading "Decision round N — Claude merged options" --file messages/06-merge-rN.md`. Show the user this Part 4, and stop.

   If one request covers both decisions no round has ranked and decisions already merged, run two rounds, one after the other: the ranking round first, then the follow-up round. If the ranking round succeeds, go straight on to the follow-up round without stopping, and show the results of both at the end.
7. Finalize when the user gives their decisions or says to finalize:
   1. Write `.reviews/$0/decided-plan.md` in the format below. A fresh session with no history must be able to implement it from this file alone, so name the files and functions each group changes. A decision the user didn't answer takes your latest recommendation: the top option in the latest Part 4 that covers it, or a change you've told the user about since.
   2. Count the `### Group` headings under Implement now. If there are more than 15, don't append yet. Tell the user: "This plan has <count> groups, more than 15, which usually means the change is too big for one review. Choose one: 1. Proceed with all <count> groups. 2. Defer lower-priority groups. 3. Split off a follow-up change." Then stop. For choice 1, go on to the next step. For choice 2 or 3, move the groups the user names to Deferred. If they name none, move groups you choose until 15 or fewer remain. Each moved group becomes one line under Deferred, in that section's format: its title and Step 5 items, marked "lower priority" (choice 2) or "follow-up change" (choice 3), and when to promote it. Then repeat this step.
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
