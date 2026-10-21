# Does memory help?

A synthetic benchmark of what each memory configuration lets an agent answer.
`python examples/memory_benchmark.py` writes `<date>.json` here; the tables below
are from `2026-10-21.json`. 20 users chat with an assistant over 8, 32, or 64
sessions each. Facts come up in passing, one is a request to remember ("Remember
that my locker code is 4412"), 40% of users move cities later, and the rest is
chatter, including distractors that look like facts ("I like long walks", "My
friend lives in Rome"). Half the statements are in the forms the built-in
extractor is built for ("my dog is named Rex") and half are paraphrases it cannot
parse ("Our dog goes by the name Rex"); search can find both. Afterwards each user
is asked their current city, their dog's name, their drink, and their locker
code. Means over 3 seeds.

No model is involved. A configuration builds a context for the question from its
memory, and a recency-aware reader answers from it: the answer is the
latest-mentioned of the values the user ever gave, and it is right if that is the
true current one. This measures what memory retrieves and how clean it is, not
how well a model reads. It is a sanity check on synthetic data, not a benchmark on
recorded conversations.

Configurations: `none`; full history (everything the user said); episodic search
(the best 3 things the user said); notes saved by tool call (when the user asks to
remember something); notes from dreaming (an offline pass with the built-in rule
extractor); both; and both tiers (3 items from each). The rows marked * replace
the built-in extractor with an oracle that parses every phrasing the workload uses.
That is an upper bound on extraction, which a good model-backed extractor would
approach, and it shows what the pipeline delivers when extraction is perfect.

<div align="center">

| Configuration | 8 sessions | 32 sessions | 64 sessions | Context words at 64 |
|:--|:--:|:--:|:--:|--:|
| none | 0.000 | 0.000 | 0.000 | 0 |
| full history in context | 1.000 | 1.000 | 1.000 | 1,358 |
| episodic search only | 0.958 | 0.958 | 0.958 | 20 |
| notes saved by tool call only | 0.154 | 0.133 | 0.113 | 3 |
| notes from dreaming only | 0.492 | 0.512 | 0.487 | 15 |
| notes from tools and dreaming | 0.492 | 0.512 | 0.487 | 15 |
| notes from dreaming, oracle extractor * | 1.000 | 1.000 | 1.000 | 12 |
| **episodic + notes** | **0.942** | **0.971** | **0.958** | 35 |
| **episodic + notes, oracle extractor *** | **1.000** | **1.000** | **1.000** | 33 |

</div>

Accuracy by attribute at 64 sessions per user:

| Configuration | city | dog | drink | locker |
|:--|--:|--:|--:|--:|
| episodic search only | 0.83 | 1.00 | 1.00 | 1.00 |
| notes saved by tool call only | 0.00 | 0.00 | 0.00 | 0.45 |
| notes from dreaming only | 0.42 | 0.55 | 0.53 | 0.45 |
| notes from dreaming, oracle extractor * | 1.00 | 1.00 | 1.00 | 1.00 |
| episodic + notes | 0.83 | 1.00 | 1.00 | 1.00 |
| episodic + notes, oracle extractor * | 1.00 | 1.00 | 1.00 | 1.00 |

## What it shows

- **The full history is not needed.** Search over the record reads about 20 words per question where the full history needs 196, 694, and 1,358 as it grows from 8 to 64 sessions, and scores 0.96. The cost of keeping the record is nothing at answer time.
- **Episodic search alone fails on facts that change.** It is perfect on facts said once (dog, drink, locker) and gets city right 83% of the time, because the move often shares few words with the question, so the old statement is retrieved instead of the new one.
- **The notes fix that only if extraction is good.** With a perfect extractor, the notes score 1.000 at every history length with 12 words of context: the later statement supersedes the old note, so the note is current and the reader sees one value. With the built-in rule extractor they score about 0.49, because it parses only half of the phrasings. The pipeline is not the limit; the extractor is. A model-backed `Extractor` is the obvious next step, and a real extractor lands between the two rows.
- **With the built-in extractor, the notes add little to search.** Both tiers together (0.958 at 64 sessions) roughly match search alone (0.958), slightly better at 32 sessions (0.971), slightly worse at 8 (0.942). This benchmark does not show the rule extractor earning its keep over a good search, and the table should not be read as if it did.
- **Tools alone remember what they are told to.** Saving by tool call catches only the explicit request, and only when phrased as the agent is built to recognize (locker: 0.45), so accuracy is about 0.11 to 0.15.
- **Cost of building.** Dreaming adds about 100 to 150 log records for 1,280 sessions (the notes and the pass record), against about 10,400 records the sessions themselves wrote.

## What changed along the way, and a caveat

The first version of this benchmark stated every fact in the phrasing the built-in
extractor is built for, and derived memory scored 1.000. That was circular: the
workload and the extractor were written by the same hand to match. Half the
statements are now paraphrases, and the rule extractor drops to about 0.49.

The paraphrases favour search. They were written to share a stem with the question
("Our dog goes by the name Rex" for "the name of the dog"; "where I have lived" for
"where does X live"), which the stemming lexical index matches, so episodic search
is strong here. A paraphrase with no shared words ("I am based in Lyon these days")
would defeat a lexical index and need an embedding index (`VectorIndex`) or a
better query; this benchmark does not test that. Read the search-only rows as
"search when the wording overlaps", not as search in general.
