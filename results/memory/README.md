# Does memory help?

A synthetic benchmark of what each memory configuration lets an agent answer.
`python examples/memory_benchmark.py` writes `<date>.json` here; the tables below
are from `2026-10-21.json`. 20 users chat with an assistant over 8, 32, or 64
sessions each. Facts come up in passing ("I live in Lyon"), one is a request to
remember ("Remember that my locker code is 4412"), 40% of users move cities
later, and the rest is chatter, including distractors that look like facts
("I like long walks", "My friend lives in Rome"). Afterwards each user is asked
their current city, their dog's name, their drink, and their locker code. Means
over 3 seeds.

No model is involved. A configuration builds a context for the question from its
memory, and a recency-aware reader answers from it: the answer is the
latest-mentioned of the values the user ever gave, and it is right if that is the
true current one. This measures what memory retrieves and how clean it is, not
how well a model reads. It is a sanity check on synthetic data, not a benchmark on
recorded conversations.

Configurations: `none`; `full context` (everything the user said); `episodic` (the
best 3 things the user said, by search); `derived, online tools` (notes the agent
saved by tool call when asked to remember); `derived, dreaming` (notes an offline
pass extracted); `derived, both`; and `episodic + derived` (3 from each tier, with
online tools and dreaming).

<div align="center">

| Configuration | 8 sessions | 32 sessions | 64 sessions | Context words at 64 |
|:--|:--:|:--:|:--:|--:|
| none | 0.000 | 0.000 | 0.000 | 0 |
| full context | 1.000 | 1.000 | 1.000 | 1,354 |
| episodic | 0.917 | 0.892 | 0.892 | 19 |
| derived, online tools | 0.250 | 0.250 | 0.250 | 6 |
| derived, dreaming | 0.808 | 0.892 | 0.921 | 13 |
| derived, both | 0.775 | 0.871 | 0.875 | 14 |
| **episodic + derived** | **0.975** | **0.988** | **0.983** | 33 |

</div>

Accuracy by attribute at 64 sessions per user:

| Configuration | city | dog | drink | locker |
|:--|--:|--:|--:|--:|
| episodic | 0.57 | 1.00 | 1.00 | 1.00 |
| derived, online tools | 0.00 | 0.00 | 0.00 | 1.00 |
| derived, dreaming | 0.92 | 1.00 | 0.77 | 1.00 |
| derived, both | 0.78 | 1.00 | 0.72 | 1.00 |
| episodic + derived | 0.93 | 1.00 | 1.00 | 1.00 |

## What it shows

- **The tiers answer different questions.** Episodic search alone is good at facts said once (dog, drink, locker: 1.00) and poor at facts that change (city: 0.57), because the move ("Big news: I moved to Paris") shares no words with the question, so search often returns the old statement. Dreaming resolves that: the later statement supersedes the old note, and city rises from 0.57 to 0.92 as the history grows. Both tiers together reach 0.98.
- **Memory is cheap where full context is not.** The full history is perfect here, since a recency-aware reader gets every statement, but costs 192 words per question at 8 sessions and 1,354 at 64. The two tiers together read 33 words at every history length: 41 times fewer at 64 sessions, for 1.7 points of accuracy.
- **Tools alone remember what they are told to.** An agent that saves notes by tool call catches the explicit request (locker: 1.00) and nothing it was not asked to save, so accuracy is 0.25. Dreaming finds the implied facts.
- **More notes is not always better.** Adding online notes to dreaming lowers accuracy (0.921 to 0.875 at 64): the locker fact now exists twice, and the extra notes crowd the top three for the same user. A lexical index without stemming also cannot match "live" to "lives" or "prefer" to "prefers", which is why derived drink is 0.77 when distractor preferences ("I like long walks") compete with the real one. Deduplication across paths and a stemming or embedding index are the obvious next steps.
- **Cost of building.** Dreaming adds about 170 log records for 160 sessions (the notes and the pass record), against about 1,450 records the sessions themselves wrote.
