"""RAG-grounded DJ text generation.

Grounding contract (PRD 6.2):
* The AI DJ may only state facts present in the track's authored metadata timeline
  or its acoustic profile.
* Every answer/announcement returns the metadata entries it was grounded in
  (``sourceEvents``), so the claim is auditable in the UI and in tests.
* If retrieval finds nothing relevant, the DJ says so instead of inventing facts.

A hosted OpenAI-compatible model can phrase the copy when ``OPENAI_API_KEY`` is
configured; its output is accepted only after the same grounding validation.
Otherwise a deterministic composer produces the text, so the engine works fully
offline.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

import httpx

from ..config import settings

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "being", "to", "of", "in", "on", "for", "with", "about", "from", "this", "that",
    "these", "those", "it", "its", "what", "which", "who", "whom", "where", "when",
    "why", "how", "tell", "me", "does", "do", "did", "can", "could", "would", "you",
    "your", "i", "my", "we", "our", "track", "song", "please", "know", "any", "some",
}

_TYPE_HINTS = {
    "sample_credit": {"sample", "samples", "sampled", "samplex", "chop", "chops", "vocal", "record", "origin", "cover", "interpolation"},
    "trivia": {"trivia", "story", "recorded", "session", "fun", "fact", "behind", "history"},
    "artist_bio": {"artist", "producer", "produced", "who", "bio", "career", "label", "made"},
    "glossary": {"mean", "means", "glossary", "term", "phrase", "definition"},
}

_RELATIVE_EMOJI = {"identical": "same key", "adjacent": "one step around the wheel", "relative": "the relative major/minor"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in _STOPWORDS}


def _timeline_as_dicts(track) -> list[dict[str, Any]]:
    return [
        {
            "eventId": event.id,
            "ts": event.ts_seconds,
            "type": event.event_type,
            "text": event.text,
        }
        for event in track.timeline
    ]


def _acoustic_profile_source(track) -> dict[str, Any]:
    return {
        "eventId": f"profile:{track.id}",
        "ts": 0,
        "type": "acoustic_profile",
        "text": (
            f"Authored acoustic profile for {track.title}: {track.bpm:g} BPM, "
            f"Camelot {track.camelot_key}, energy {track.energy:.2f}, valence {track.valence:.2f}."
        ),
    }


def retrieve_events(track, question: str, limit: int = 3) -> list[dict[str, Any]]:
    """Keyword-overlap retrieval over the track's authored metadata timeline."""
    events = _timeline_as_dicts(track)
    profile = _acoustic_profile_source(track)
    events.append(profile)

    query_tokens = _tokens(question)
    hinted_types = {
        etype for etype, hints in _TYPE_HINTS.items() if query_tokens & hints
    }
    # A question about the track's sound legitimately targets the acoustic profile.
    if query_tokens & {"bpm", "tempo", "key", "camelot", "energy", "valence", "fast", "slow"}:
        hinted_types.add("acoustic_profile")

    scored: list[tuple[float, dict[str, Any]]] = []
    for event in events:
        event_tokens = _tokens(event["text"]) | _tokens(event["type"].replace("_", " "))
        overlap = len(query_tokens & event_tokens)
        score = overlap + (1.5 if event["type"] in hinted_types else 0.0)
        if score > 0:
            scored.append((score, event))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    selected = [event for _, event in scored[:limit]]
    if not selected and not query_tokens:
        selected = [profile]
    return selected


def _numbers(text: str) -> set[str]:
    return {m.lstrip("0") or "0" for m in re.findall(r"\d+(?:\.\d+)?", text or "")}


def _validation_corpus(track, sources: Sequence[dict[str, Any]], extra: Sequence = ()) -> tuple[set[str], set[str]]:
    corpus = " ".join(
        [track.title, track.artist, track.genre, *(s["text"] for s in sources), *(str(x) for x in extra)]
    )
    return _tokens(corpus), _numbers(corpus)


def _is_grounded(text: str, track, sources: Sequence[dict[str, Any]], extra: Sequence = ()) -> bool:
    if not text or len(text) < 8:
        return False
    allowed_tokens, allowed_numbers = _validation_corpus(track, sources, extra)
    content_tokens = _tokens(text)
    if not content_tokens:
        return False
    unknown_numbers = _numbers(text) - allowed_numbers
    if unknown_numbers:
        return False
    overlap = len(content_tokens & allowed_tokens) / len(content_tokens)
    return overlap >= 0.45


async def _call_llm(system: str, user: str) -> str | None:
    if not settings.LLM_ENABLED:
        return None
    url = settings.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                json={
                    "model": settings.LLM_MODEL,
                    "temperature": 0.4,
                    "max_tokens": 160,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception:  # noqa: BLE001 - any provider failure falls back to deterministic text
        return None


_FACT_LABELS = {
    "sample_credit": "Sample note:",
    "trivia": "Trivia:",
    "artist_bio": "Artist note:",
    "glossary": "Glossary:",
}


def _pick_grounding_line(track, sources: Sequence[dict[str, Any]]) -> tuple[str, str] | None:
    """Prefer concrete credits/trivia over a generic artist bio."""
    for preferred in ("sample_credit", "trivia", "artist_bio", "glossary"):
        for source in sources:
            if source["type"] == preferred and source["text"]:
                return _FACT_LABELS[preferred], source["text"]
    return None


def _clip(text: str, limit: int = 150) -> str:
    text = text.strip().rstrip(".")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _transition_template(
    current,
    upcoming,
    match_reason: dict[str, Any],
    sentiment: float,
    sources: Sequence[dict[str, Any]],
) -> str:
    relation = match_reason.get("camelotRelation", "identical")
    delta = float(match_reason.get("bpmDeltaPct", 0.0))
    energy = float(match_reason.get("energyFit", 0.5))
    tempo_relation = match_reason.get("tempoRelation", "direct")

    if delta <= 0.5:
        tempo_phrase = "a locked-in, near-identical tempo"
    elif tempo_relation == "double_time":
        tempo_phrase = f"a clean double-time lift ({delta:g}% off the doubled tempo)"
    elif tempo_relation == "half_time":
        tempo_phrase = f"a half-time pull-back ({delta:g}% off the halved tempo)"
    else:
        tempo_phrase = f"only {delta:g}% off the tempo"

    if relation == "identical":
        key_phrase = f"staying dead centre in {upcoming.camelot_key}"
    elif relation == "relative":
        key_phrase = f"flipping to {upcoming.camelot_key}, the relative major/minor of {current.camelot_key}"
    else:
        key_phrase = f"stepping one notch around the wheel from {current.camelot_key} to {upcoming.camelot_key}"

    if sentiment >= 0.25:
        mood_phrase = "the room is up, so the energy climbs"
    elif sentiment <= -0.25:
        mood_phrase = "the room needs air, so I'm easing the energy back"
    else:
        mood_phrase = "holding the groove steady"

    facts = _pick_grounding_line(upcoming, sources)
    fact_phrase = f" {facts[0]} {_clip(facts[1])}." if facts else ""
    return (
        f"Taking you from {current.artist} into {upcoming.artist}'s \"{upcoming.title}\" — "
        f"{tempo_phrase}, {key_phrase}. {mood_phrase[0].upper()}{mood_phrase[1:]} "
        f"(energy fit {energy:.2f}).{fact_phrase}"
    )


async def compose_transition(
    current,
    upcoming,
    match_reason: dict[str, Any],
    sentiment: float,
    verbosity: str = "normal",
) -> dict[str, Any]:
    """Generate the AI DJ's transition line, grounded in the upcoming track's metadata."""
    sources = _timeline_as_dicts(upcoming)[:3]

    if verbosity == "quiet":
        text = (
            f"Slipping into \"{upcoming.title}\" — {match_reason.get('camelotRelation')} key, "
            f"{match_reason.get('bpmDeltaPct')}% BPM delta."
        )
        return {"text": text, "sourceEvents": [s["eventId"] for s in sources], "generator": "template"}

    template = _transition_template(current, upcoming, match_reason, sentiment, sources)
    llm_text = None
    if settings.LLM_ENABLED:
        facts = "\n".join(f"[{s['eventId']}] ({s['type']}) {s['text']}" for s in sources)
        llm_text = await _call_llm(
            "You are an AI DJ on a live radio show. Write ONE short, warm transition line "
            "(max 40 words) using ONLY the supplied facts. Never invent facts, numbers or names "
            "that are not in the facts. Return plain text only.",
            (
                f"Current track: {current.artist} - {current.title} "
                f"({current.bpm:g} BPM, {current.camelot_key}).\n"
                f"Next track: {upcoming.artist} - {upcoming.title} "
                f"({upcoming.bpm:g} BPM, {upcoming.camelot_key}).\n"
                f"Mix analysis: key relation {match_reason.get('camelotRelation')}, "
                f"BPM delta {match_reason.get('bpmDeltaPct')}%, energy fit {match_reason.get('energyFit')}.\n"
                f"Room sentiment: {sentiment:.2f}.\nFacts:\n{facts}"
            ),
        )

    if llm_text and _is_grounded(llm_text, upcoming, sources, extra=[current.title, current.artist]):
        return {
            "text": llm_text,
            "sourceEvents": [s["eventId"] for s in sources],
            "generator": "llm",
        }
    return {
        "text": template,
        "sourceEvents": [s["eventId"] for s in sources],
        "generator": "template",
    }


async def ground_answer(track, question: str, current_ts: int = 0) -> dict[str, Any]:
    """Answer a listener question from the track's metadata only."""
    sources = retrieve_events(track, question)
    if not sources:
        return {
            "answer": (
                f"I don't have anything in the crate notes for \"{track.title}\" that answers that. "
                "I can only speak to the credits and trivia in the metadata I was given."
            ),
            "sourceEvents": [],
            "citations": [],
            "grounded": False,
            "generator": "template",
        }

    lead = sources[0]
    if lead["type"] == "acoustic_profile":
        answer = f"On the profile for \"{track.title}\": {lead['text'].split(': ', 1)[-1]}"
    else:
        answer = f"From the crate notes on \"{track.title}\": {lead['text']}"

    llm_text = None
    if settings.LLM_ENABLED:
        facts = "\n".join(f"[{s['eventId']}] ({s['type']}) {s['text']}" for s in sources)
        llm_text = await _call_llm(
            "You are an AI DJ. Answer the listener in 1-2 sentences using ONLY the supplied "
            "facts about this track. If the facts do not answer the question, say you don't "
            "have that in the crate notes. Never invent facts. Plain text only.",
            f"Track: {track.artist} - {track.title}.\nQuestion: {question}\nFacts:\n{facts}",
        )

    if llm_text and _is_grounded(llm_text, track, sources):
        answer = llm_text
        generator = "llm"
    else:
        generator = "template"

    return {
        "answer": answer,
        "sourceEvents": [s["eventId"] for s in sources],
        "citations": sources,
        "grounded": True,
        "generator": generator,
    }


def battle_announcement(winner, loser, winner_votes: int, loser_votes: int) -> dict[str, Any]:
    sources = _timeline_as_dicts(winner)[:2]
    text = (
        f"Soundclash closed: \"{winner.title}\" by {winner.artist} takes it "
        f"{winner_votes}-{loser_votes} over \"{loser.title}\". "
        f"Spliced straight into the queue."
    )
    fact = _pick_grounding_line(winner, sources)
    if fact:
        text += f" {fact[0]} {_clip(fact[1])}."
    return {"text": text, "sourceEvents": [s["eventId"] for s in sources], "generator": "template"}


def taste_summary(top_keys: Iterable[str], bpm_min: float, bpm_max: float, energy: float) -> str:
    keys = ", ".join(sorted(set(top_keys))) or "open format"
    return (
        f"Your taste leans {keys} at {bpm_min:.0f}-{bpm_max:.0f} BPM with an average energy of "
        f"{energy:.2f} — I'll keep that in the crate when I build your next set."
    )
