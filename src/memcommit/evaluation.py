"""Does memory help? A deterministic benchmark of what each memory configuration lets an agent answer.

    workload = make_workload(users=20, sessions=8, seed=0)
    for result in evaluate(workload):
        print(result.name, result.accuracy, result.context_words)

The workload is synthetic: users chat with an assistant over many sessions.
Facts are stated in passing ("I live in Lyon"), one is stated as a request
to remember ("Remember that my locker code is 4412"), some change later ("I
moved to Paris"), and the rest is chatter, including distractors that look like
facts. Half the statements are paraphrases the built-in extractor cannot parse
("Our dog goes by the name Rex"), so it is not graded only on text built for it;
search can still find them. Afterwards each user is asked for their current city, their dog's name,
their drink, and their locker code.

No model is involved. A configuration builds a context for the question from
its memory, and a recency-aware reader answers from it: the answer is the
latest-mentioned of the values the user ever gave for that attribute, and it is
right if that is the true current one. So the benchmark measures what memory
retrieves and how clean it is, not how well a model reads.

Configurations:

- `none`: nothing is retrieved.
- `full context`: everything the user said, in order. The upper bound on what is
  retrievable, and the cost of not having memory at all.
- `episodic`: the best few things the user said, found by search.
- `derived, online tools`: notes the agent saved with its tools during sessions,
  which in this benchmark is when the user asks it to remember something.
- `derived, dreaming`: notes an offline pass extracted from the sessions.
- `derived, both`: online tools and dreaming.
- `episodic + derived`: the best few of each tier.
- The `oracle` configurations replace the built-in extractor with one that parses
  every phrasing the workload uses. They are an upper bound on extraction, what a
  good model-backed extractor would approach, and show what the notes deliver when
  extraction is perfect.

Each result reports accuracy, the words of context the reader was given, and the
log records the memory cost to build. For the last configuration it also reports
which tier supplied each correct answer.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .dream import Dreamer, Extractor, Learning, heuristic_extractor
from .episodes import Session
from .knowledge import KNOWLEDGE, WORKING, Knowledge
from .recall import Recall
from .records import SHARED
from .store import MemoryStore
from .traces import MESSAGE

CITIES = ["Lyon", "Oslo", "Kyoto", "Lima", "Porto", "Tartu", "Quito", "Brno", "Hanoi", "Cork", "Bergen", "Turin"]
DOGS = ["Rex", "Miso", "Biscuit", "Juno", "Pepper", "Waffles", "Otto", "Nova", "Kiwi", "Bruno", "Maple", "Sunny"]
DRINKS = ["oat milk", "green tea", "espresso", "ginger ale", "cold brew", "mint tea", "cocoa", "kombucha"]
NOISE = [
    "How do I reset my router?",
    "Can you summarise the meeting notes from yesterday?",
    "What is a good way to learn the piano?",
    "I love a good thriller on a rainy day.",
    "My friend lives in Rome and says hello.",
    "The weather has been strange this week.",
    "Please draft a polite email to the landlord.",
    "I like long walks, mostly.",
    "My manager is travelling next week.",
    "What time is it in Tokyo?",
    "Thanks, that helps a lot.",
    "Remind me what a mutex is.",
]
ATTRIBUTES = {
    "city": "Where does {user} live?",
    "dog": "What is the name of {user}'s dog?",
    "drink": "What does {user} prefer?",
    "locker": "What is {user}'s locker code?",
}


@dataclass(frozen=True)
class Question:
    user: str
    attribute: str
    text: str
    gold: str
    values: tuple[str, ...]  # every value the user ever gave for the attribute, gold included


@dataclass
class Workload:
    sessions: list[tuple[str, list[str]]]  # (user, user turns), in order
    questions: list[Question]


def make_workload(users: int = 20, sessions: int = 8, seed: int = 0, paraphrase: float = 0.5) -> Workload:
    """`paraphrase` is the share of facts stated in words the built-in extractor cannot parse ("Our dog goes by the
    name Rex") rather than in the forms it is built for ("my dog is named Rex"). Search can still find both."""
    rng = random.Random(seed)

    def say(plain: str, other: str) -> str:
        return other if rng.random() < paraphrase else plain

    names = [f"user{n:02d}" for n in range(users)]
    plan: dict[str, list[list[str]]] = {name: [[] for _ in range(sessions)] for name in names}
    questions: list[Question] = []
    for name in names:
        moved = sessions >= 5 and rng.random() < 0.4
        cities = rng.sample(CITIES, 2 if moved else 1)
        dog, drink, code = rng.choice(DOGS), rng.choice(DRINKS), str(rng.randint(1000, 9999))
        slots = rng.sample(range(sessions), 5 if moved else 4)
        city_slots = sorted(slots[:2]) if moved else slots[:1]
        dog_slot, drink_slot, locker_slot = slots[-3:]
        facts = [
            (say(f"I live in {cities[0]}.", f"{cities[0]} is where I have lived for years."), city_slots[0]),
            (say(f"By the way, my dog is named {dog}.", f"Our dog goes by the name {dog}."), dog_slot),
            (say(f"I prefer {drink}.", f"Preferring {drink} over everything else, honestly."), drink_slot),
            (
                say(f"Remember that my locker code is {code}.", f"Please keep in mind the locker code, it is {code}."),
                locker_slot,
            ),
        ]
        if moved:
            moved_line = say(f"Big news: I moved to {cities[1]}.", f"{cities[1]} is where I have lived since the move.")
            facts.append((moved_line, city_slots[1]))
        for line, at in facts:
            plan[name][at].append(line)
        gold = {"city": cities[-1], "dog": dog, "drink": drink, "locker": code}
        values = {"city": tuple(cities), "dog": (dog,), "drink": (drink,), "locker": (code,)}
        for attribute, template in ATTRIBUTES.items():
            questions.append(Question(name, attribute, template.format(user=name), gold[attribute], values[attribute]))
    order: list[tuple[str, list[str]]] = []
    for index in range(sessions):
        for name in names:
            turns = rng.sample(NOISE, 3)
            for fact in plan[name][index]:
                turns.insert(rng.randrange(len(turns) + 1), fact)
            order.append((name, turns))
    return Workload(order, questions)


def _explicit(text: str) -> str | None:
    found = re.search(r"\bremember that ([^.!?]+)", text, re.I)
    return found.group(1).strip() if found else None


_ORACLE = {
    "city": [r"I (?:live in|moved to) ([A-Z]\w+)", r"([A-Z]\w+) is where I have lived"],
    "dog": [r"my dog is named (\w+)", r"Our dog goes by the name (\w+)"],
    "drink": [r"I prefer ([a-z ]+?)\.", r"Preferring ([a-z ]+?) over"],
    "locker": [r"locker code is (\d+)", r"locker code, it is (\d+)"],
}
_PHRASE = {
    "city": "{u} lives in {v}.",
    "dog": "{u}'s dog is {v}.",
    "drink": "{u} prefers {v}.",
    "locker": "{u}'s locker code is {v}.",
}


def oracle_extractor(session: Session) -> list[Learning]:
    """An extractor that parses every phrasing the workload uses: an upper bound on extraction, what a good model
    would approach. It is told what to look for, so it shows what the notes deliver when extraction is perfect."""
    out = []
    for turn in session.transcript():
        if turn["role"] != "user":
            continue
        for attribute, patterns in _ORACLE.items():
            for pattern in patterns:
                for value in re.findall(pattern, turn["text"]):
                    text = _PHRASE[attribute].format(u=session.user, v=value)
                    out.append(Learning(f"user/{session.user}/{attribute}", text, sources=(turn["id"],)))
    return out


def build(
    workload: Workload, online: bool, dream: bool, extractor: Extractor = heuristic_extractor
) -> tuple[MemoryStore, Knowledge]:
    """Run every session through a store. With `online`, the agent saves what users ask it to remember, by tool
    call, as it goes. With `dream`, an offline pass reads all the sessions afterwards."""
    store = MemoryStore()
    notes = Knowledge(store, "assistant")
    for user, turns in workload.sessions:
        with Session.start(store, "assistant", user=user) as session:
            for turn in turns:
                session.user_says(turn)
                request = _explicit(turn) if online else None
                if request:
                    path = f"user/{user}/remembered/{re.sub(r'[^a-z0-9]+', '-', request.lower()).strip('-')[:40]}"
                    notes.call("add_memory", {"path": path, "text": f"{user}: {request}."}, session=session)
                session.agent_says("Noted.")
    if dream:
        Dreamer(store, extractor=extractor).run()
    return store, notes


@dataclass
class Context:
    items: list[tuple[str, str, int]] = field(default_factory=list)  # (tier, text, lsn)

    def text(self) -> str:
        return "\n".join(t for _, t, _ in sorted(self.items, key=lambda i: i[2]))

    def words(self) -> int:
        return len(self.text().split())


def _episodic(store: MemoryStore, recall: Recall, question: Question, k: int) -> list[tuple[str, str, int]]:
    hits = recall.search(
        question.text, k, kinds=[MESSAGE], where={"user": question.user, "role": "user"}, tiers=None, touch=False
    )
    return [("episodic", h.memory.body["content"], store.state.first[h.memory.id].lsn) for h in hits]


def _derived(store: MemoryStore, recall: Recall, question: Question, k: int) -> list[tuple[str, str, int]]:
    hits = recall.search(question.text, 200, kinds=[KNOWLEDGE], scopes=[SHARED, "assistant"], tiers=WORKING)
    mine = [h for h in hits if h.memory.key.startswith(f"user/{question.user}/")][:k]
    return [("derived", h.memory.body["text"], store.state.first[h.memory.id].lsn) for h in mine]


def _everything(store: MemoryStore, question: Question) -> list[tuple[str, str, int]]:
    found = store.find(kind=MESSAGE, where={"user": question.user, "role": "user"})
    return [("episodic", m.body["content"], store.state.first[m.id].lsn) for m in found]


def answer(context: Context, question: Question) -> bool:
    """The recency-aware reader: right if the latest-mentioned value the user ever gave is the current one."""
    text = context.text().lower()
    last = {v: text.rfind(v.lower()) for v in question.values}
    return last[question.gold] >= 0 and last[question.gold] == max(last.values())


@dataclass
class Result:
    name: str
    accuracy: float
    by_attribute: dict[str, float]
    context_words: float
    records: int
    answered_from: dict[str, int] = field(default_factory=dict)


def _score(name: str, workload: Workload, store: MemoryStore, retrieve: Callable[[Question], Context]) -> Result:
    right: dict[str, list[bool]] = {a: [] for a in ATTRIBUTES}
    words, tiers = [], {"derived": 0, "episodic": 0, "both": 0}
    for question in workload.questions:
        context = retrieve(question)
        ok = answer(context, question)
        right[question.attribute].append(ok)
        words.append(context.words())
        if ok:
            from_tier = {t for t, text, _ in context.items if question.gold.lower() in text.lower()}
            tiers["both" if len(from_tier) == 2 else next(iter(from_tier))] += 1
    flat = [x for v in right.values() for x in v]
    return Result(
        name,
        sum(flat) / len(flat),
        {a: sum(v) / len(v) for a, v in right.items()},
        sum(words) / len(words),
        store.lsn,
        {k: v for k, v in tiers.items() if v},
    )


def evaluate(workload: Workload, k: int = 3) -> list[Result]:
    """Score every configuration on `workload`; `k` is how many items each tier contributes to a context."""
    results = []
    empty = MemoryStore()
    results.append(_score("none", workload, empty, lambda q: Context()))
    store_e, _ = build(workload, online=False, dream=False)
    recall_e = Recall(store_e)
    results.append(_score("full context", workload, store_e, lambda q: Context(_everything(store_e, q))))
    results.append(_score("episodic", workload, store_e, lambda q: Context(_episodic(store_e, recall_e, q, k))))
    for name, online, dream, extractor in (
        ("derived, online tools", True, False, heuristic_extractor),
        ("derived, dreaming", False, True, heuristic_extractor),
        ("derived, both", True, True, heuristic_extractor),
        ("derived, dreaming, oracle extractor", False, True, oracle_extractor),
    ):
        store, _ = build(workload, online, dream, extractor)
        recall = Recall(store)
        results.append(_score(name, workload, store, lambda q, s=store, r=recall: Context(_derived(s, r, q, k))))
    for name, extractor in (
        ("episodic + derived", heuristic_extractor),
        ("episodic + derived, oracle", oracle_extractor),
    ):
        store, _ = build(workload, True, True, extractor)
        recall = Recall(store)
        results.append(
            _score(
                name,
                workload,
                store,
                lambda q, s=store, r=recall: Context(_episodic(s, r, q, k) + _derived(s, r, q, k)),
            )
        )
    return results


def table(results: list[Result]) -> str:
    """The results as a Markdown table."""
    attributes = list(ATTRIBUTES)
    head = "| Configuration | Accuracy | " + " | ".join(attributes) + " | Context words | Log records |"
    rows = [head, "|:--|--:|" + "--:|" * len(attributes) + "--:|--:|"]
    for r in results:
        cells = " | ".join(f"{r.by_attribute[a]:.2f}" for a in attributes)
        rows.append(f"| {r.name} | {r.accuracy:.3f} | {cells} | {r.context_words:.0f} | {r.records:,} |")
    return "\n".join(rows)
