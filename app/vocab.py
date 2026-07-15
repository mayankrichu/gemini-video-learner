from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import spacy
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from .config import Settings, get_settings
from .models import AIOutputItem

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "v2"

A1_A2_VERBS = {
    "sein",
    "haben",
    "werden",
    "gehen",
    "kommen",
    "machen",
    "tun",
    "leben",
    "geben",
    "nehmen",
    "sehen",
    "sagen",
    "sprechen",
    "lernen",
    "arbeiten",
    "fahren",
    "spielen",
    "lesen",
    "schreiben",
    "denken",
    "wissen",
    "können",
    "müssen",
    "wollen",
    "möchten",
    "dürfen",
    "sollen",
    "heißen",
    "essen",
    "trinken",
    "brauchen",
    "finden",
    "fragen",
    "antworten",
    "hören",
    "kaufen",
    "bezahlen",
    "wohnen",
    "lieben",
    "stehen",
    "liegen",
    "sitzen",
    "stellen",
    "legen",
    "setzen",
    "öffnen",
    "zeigen",
}

A1_A2_NOUNS = {
    "mann",
    "frau",
    "kind",
    "tag",
    "jahr",
    "haus",
    "schule",
    "arbeit",
    "zeit",
    "wasser",
    "essen",
    "name",
    "stadt",
    "land",
    "freund",
    "familie",
}


@dataclass
class Occurrence:
    start: float
    end: float
    source_text: str


@dataclass
class AICandidate:
    id: str
    type: str
    lemma: str
    contexts: list[str] = field(default_factory=list)
    occurrences: list[Occurrence] = field(default_factory=list)

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "lemma": self.lemma.capitalize() if self.type == "noun" else self.lemma,
            "contexts": self.contexts,
        }


class AICandidateLimitError(RuntimeError):
    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"Transcript contains more than {limit} unique AI vocabulary items.")


class DictionaryLoader:
    def __init__(self, settings: Settings):
        self.settings = settings

    def load(self, blob_name: str) -> dict[str, str]:
        local_path = self._local_path(blob_name)
        if local_path and local_path.exists():
            logger.info("Loading dictionary from %s", local_path)
            return self._read_json(local_path.read_bytes(), blob_name)

        try:
            client = self._blob_service_client().get_blob_client(
                container=self.settings.azure_blob_container_name,
                blob=blob_name,
            )
            data = client.download_blob().readall()
            dictionary = self._read_json(data, blob_name)
            logger.info("Loaded %s entries from Azure blob %s", len(dictionary), blob_name)
            return dictionary
        except Exception as exc:
            raise RuntimeError(
                f"Could not load dictionary '{blob_name}' from Azure Blob Storage. "
                "Check Managed Identity permissions, the connection string, container name, "
                "and blob name."
            ) from exc

    def _local_path(self, blob_name: str) -> Path | None:
        if not self.settings.local_dictionary_dir:
            return None
        return Path(self.settings.local_dictionary_dir) / blob_name

    def _blob_service_client(self) -> BlobServiceClient:
        if self.settings.azure_storage_connection_string:
            return BlobServiceClient.from_connection_string(
                self.settings.azure_storage_connection_string
            )
        account_name = self.settings.azure_storage_account_name.strip()
        if not account_name:
            raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME is not configured.")
        return BlobServiceClient(
            account_url=f"https://{account_name}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
        )

    @staticmethod
    def _read_json(raw: bytes, name: str) -> dict[str, str]:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError(f"{name} must contain a JSON object.")
        return {
            normalize_word(str(key)): str(translation).strip()
            for key, translation in value.items()
            if normalize_word(str(key)) and str(translation).strip()
        }


class VocabService:
    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            self.nlp = spacy.load("de_core_news_sm")
        except Exception as exc:
            raise RuntimeError(
                "Could not load spaCy model 'de_core_news_sm'. Install the model from "
                "requirements.txt before starting the API."
            ) from exc

        loader = DictionaryLoader(settings)
        self.verb_translations = loader.load("verb_translations.json")
        self.noun_translations = loader.load("noun_translations.json")
        self.connector_translations = loader.load("connector_translations.json")

    def dictionary_sizes(self) -> dict[str, int]:
        return {
            "verbs": len(self.verb_translations),
            "nouns": len(self.noun_translations),
            "connectors": len(self.connector_translations),
        }

    def analyze_free(
        self,
        transcript: list[dict[str, float | str]],
        selected_types: Iterable[str],
    ) -> list[dict[str, Any]]:
        selected = set(selected_types)
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str, float]] = set()

        texts = [str(block["text"]) for block in transcript]
        for block, doc in zip(transcript, self.nlp.pipe(texts, batch_size=64), strict=True):
            text = str(block["text"])
            start = float(block["start"])
            end = max(start + float(block["duration"]), start + 2.8)

            for token in doc:
                lemma = normalize_word(token.lemma_)
                if not lemma or not token.is_alpha:
                    continue

                item: dict[str, Any] | None = None
                if (
                    "verb" in selected
                    and token.pos_ in {"VERB", "AUX"}
                    and lemma not in A1_A2_VERBS
                    and lemma in self.verb_translations
                ):
                    item = {
                        "type": "verb",
                        "word": lemma,
                        "translation": self.verb_translations[lemma],
                        "article": None,
                    }
                elif (
                    "noun" in selected
                    and token.pos_ in {"NOUN", "PROPN"}
                    and lemma not in A1_A2_NOUNS
                    and lemma in self.noun_translations
                ):
                    item = {
                        "type": "noun",
                        "word": lemma,
                        "translation": self.noun_translations[lemma],
                        "article": article_from_token(token),
                    }
                elif (
                    "connector" in selected
                    and token.pos_ in {"CCONJ", "SCONJ", "ADV", "ADP", "PART"}
                    and lemma in self.connector_translations
                ):
                    item = {
                        "type": "connector",
                        "word": lemma,
                        "translation": self.connector_translations[lemma],
                        "article": None,
                    }

                if item is None:
                    continue

                key = (item["type"], item["word"], round(start, 1))
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    {
                        "start": start,
                        "end": end,
                        **item,
                        "example": None,
                        "example_translation": None,
                        "source_text": text,
                        "ai_generated": False,
                        "level": "B1+",
                    }
                )

        return results

    def extract_ai_candidates(
        self,
        transcript: list[dict[str, float | str]],
        selected_types: Iterable[str],
        max_candidates: int,
    ) -> list[AICandidate]:
        selected = set(selected_types) & {"noun", "verb"}
        by_key: dict[tuple[str, str], AICandidate] = {}

        texts = [str(block["text"]) for block in transcript]
        for block, doc in zip(transcript, self.nlp.pipe(texts, batch_size=64), strict=True):
            text = str(block["text"])
            start = float(block["start"])
            end = max(start + float(block["duration"]), start + 2.8)
            seen_in_block: set[tuple[str, str]] = set()

            for token in doc:
                lemma = normalize_word(token.lemma_)
                if not lemma or len(lemma) < 3 or not token.is_alpha or token.is_stop:
                    continue

                candidate_type: str | None = None
                if (
                    "verb" in selected
                    and token.pos_ in {"VERB", "AUX"}
                    and lemma not in A1_A2_VERBS
                ):
                    candidate_type = "verb"
                elif "noun" in selected and token.pos_ == "NOUN" and lemma not in A1_A2_NOUNS:
                    candidate_type = "noun"

                if candidate_type is None:
                    continue

                key = (candidate_type, lemma)
                if key in seen_in_block:
                    continue
                seen_in_block.add(key)

                candidate = by_key.get(key)
                if candidate is None:
                    if len(by_key) >= max_candidates:
                        raise AICandidateLimitError(max_candidates)
                    candidate = AICandidate(
                        id=f"v{len(by_key) + 1:05d}",
                        type=candidate_type,
                        lemma=lemma,
                    )
                    by_key[key] = candidate

                if text not in candidate.contexts and len(candidate.contexts) < 2:
                    candidate.contexts.append(text)
                candidate.occurrences.append(Occurrence(start=start, end=end, source_text=text))

        return list(by_key.values())

    @staticmethod
    def expand_ai_results(
        candidates: list[AICandidate],
        output_items: list[AIOutputItem],
        include_example: bool,
    ) -> list[dict[str, Any]]:
        outputs = {item.id: item for item in output_items}
        expected = {candidate.id for candidate in candidates}
        if set(outputs) != expected:
            missing = sorted(expected - set(outputs))
            unexpected = sorted(set(outputs) - expected)
            raise RuntimeError(
                f"OpenAI output IDs did not match the request. Missing={missing[:5]}, "
                f"unexpected={unexpected[:5]}"
            )

        expanded: list[dict[str, Any]] = []
        for candidate in candidates:
            output = outputs[candidate.id]
            for occurrence in candidate.occurrences:
                expanded.append(
                    {
                        "start": occurrence.start,
                        "end": occurrence.end,
                        "type": output.type,
                        "word": output.word,
                        "translation": output.translation,
                        "article": output.article,
                        "example": output.example if include_example else None,
                        "example_translation": (
                            output.example_translation if include_example else None
                        ),
                        "source_text": None,
                        "ai_generated": True,
                        "level": output.level,
                    }
                )

        expanded.sort(key=lambda item: (item["start"], item["type"], item["word"]))
        return expanded


def normalize_word(word: str) -> str:
    return re.sub(r"\s+", " ", (word or "").lower().strip())


def article_from_token(token: Any) -> str | None:
    values = token.morph.get("Gender")
    if not values:
        return None
    return {
        "masc": "der",
        "fem": "die",
        "neut": "das",
    }.get(values[0].lower())


@lru_cache(maxsize=1)
def get_vocab_service() -> VocabService:
    return VocabService(get_settings())
