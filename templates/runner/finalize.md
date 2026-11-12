Write the Decided Plan from the decisions below, in exactly the format that follows. A fresh session with no history must be able to implement it from this text alone, so name the files and functions each group changes. A decision the developer didn't answer takes your latest recommendation: the top option in the latest Part 4 that covers it, or a change you've told them about since. The exception is a decision whose latest Part 4 has a "Needs from you" line: if they neither supplied that fact nor chose an option, change nothing for it, list it under Deferred with that fact as the condition to promote it, and record it as `no answer; needs <fact>`. Each group's title is also its commit subject, after any prefix, so write it in the form of the branch's recent subjects (`git log --format=%s -20`), leaving out prefixes such as `fix:` or a ticket number. Describe the change in plain English, without the review's own labels, such as group or decision numbers, "Part 4" or the review's name.

If you've written a Decided Plan before in this review, this one replaces it, so write the whole plan again, not just what changed. Reply with only the plan.

Format:

## User decisions
1. <decision> → <choice, or "deferred"> — <user's words, "default accepted", or "no answer; needs <fact>">

## Implement now — one commit per group, in order
### Group 1: <commit subject, without any prefix>
- Items: <Step 5 item references>
- Change: <what changes>; Files: <paths/functions>
- Done when: <observable check>
- Depends on: <group n | none>

## Deferred
- <item> — <why>; promote if <condition>

## Dropped
- <item> — <why>

The developer's decisions:
