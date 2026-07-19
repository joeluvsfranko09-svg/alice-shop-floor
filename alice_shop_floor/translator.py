"""
ALICE Translator
================
Claude-powered translation engine for sewing instructions and shop floor text.

Uses claude-haiku (fast + cheap) by default — configurable in ALICE Settings.

Translates manufacturing-domain text accurately:
  "seam allowance", "topstitch", "bartack", "fusing", "notch", etc.

Public API
──────────
  translate_text(text, target_language, source_language="en") → str
  translate_instruction_set(steps, target_languages)           → dict[lang_code, list[str]]
  get_operator_language(operator_user)                         → str  ("en" fallback)
  translate_step_field(step_doc, target_lang)                  → str
"""

from __future__ import annotations

import json
import frappe

LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "ht": "Haitian Creole",
    "pt": "Portuguese",
    "fr": "French",
    "vi": "Vietnamese",
    "zh": "Chinese (Simplified)",
}

# Field names on SewingInstructionStep for each language
LANG_FIELD = {
    "en": "instruction_text_en",
    "es": "instruction_text_es",
    "ht": "instruction_text_ht",
    "pt": "instruction_text_pt",
    "fr": "instruction_text_fr",
    "vi": "instruction_text_vi",
    "zh": "instruction_text_zh",
}


# ── Core translation call ─────────────────────────────────────────────────────

def translate_text(text: str, target_language: str,
                   source_language: str = "en") -> str:
    """
    Translate *text* from source_language → target_language using Claude.
    Returns translated string. Falls back to original text on any error.
    """
    if not text or not text.strip():
        return text or ""
    if target_language == source_language:
        return text

    try:
        import requests
        from alice_shop_floor.alice_shop_floor.doctype.alice_settings.alice_settings import (
            get_api_key, get_model,
        )

        api_key = get_api_key()
        if not api_key:
            frappe.log_error("ALICE Settings: Anthropic API key not set",
                             "ALICE Translator")
            return text

        target_name = LANGUAGE_NAMES.get(target_language, target_language)
        source_name = LANGUAGE_NAMES.get(source_language, source_language)
        model       = get_model()

        system = (
            "You are a precise industrial translator specialising in garment "
            "manufacturing. Translate the user's text from "
            f"{source_name} to {target_name}. "
            "Preserve technical terms (seam allowance, topstitch, bartack, "
            "fusing, notch, grain line, basting, understitch, etc.) accurately. "
            "Output ONLY the translated text — no explanations, no quotes, "
            "no markdown."
        )

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      model,
                "max_tokens": 1024,
                "system":     system,
                "messages":   [{"role": "user", "content": text}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()

    except Exception as exc:
        frappe.log_error(str(exc), "ALICE Translator Error")
        return text   # graceful fallback — show original


def translate_batch(texts: list[str], target_language: str,
                    source_language: str = "en") -> list[str]:
    """
    Translate a list of strings in one API call (cheaper + faster).
    Returns list of translated strings in the same order.
    Falls back to originals on error.
    """
    if not texts:
        return texts
    if target_language == source_language:
        return texts

    try:
        import requests
        from alice_shop_floor.alice_shop_floor.doctype.alice_settings.alice_settings import (
            get_api_key, get_model,
        )

        api_key = get_api_key()
        if not api_key:
            return texts

        target_name = LANGUAGE_NAMES.get(target_language, target_language)
        source_name = LANGUAGE_NAMES.get(source_language, source_language)
        model       = get_model()

        # Build numbered list for batch translation
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        system = (
            "You are a precise industrial translator specialising in garment "
            "manufacturing. Translate each numbered item from "
            f"{source_name} to {target_name}. "
            "Preserve technical sewing terms accurately. "
            "Return ONLY the numbered translations in the same format: "
            "'1. <translation>\\n2. <translation>' etc. No extra text."
        )

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      model,
                "max_tokens": 2048,
                "system":     system,
                "messages":   [{"role": "user", "content": numbered}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()

        # Parse "1. text\n2. text\n..." back into list
        lines = raw.split("\n")
        results = list(texts)   # default: originals
        for line in lines:
            line = line.strip()
            if not line:
                continue
            dot_pos = line.find(". ")
            if dot_pos > 0:
                try:
                    idx = int(line[:dot_pos]) - 1
                    if 0 <= idx < len(results):
                        results[idx] = line[dot_pos + 2:].strip()
                except ValueError:
                    pass
        return results

    except Exception as exc:
        frappe.log_error(str(exc), "ALICE Batch Translate Error")
        return texts


# ── Instruction set helpers ───────────────────────────────────────────────────

def translate_instruction_step(step_doc, target_languages: list[str]) -> None:
    """
    Translate a SewingInstructionStep doc's instruction_text_en into all
    target_languages and save the result back onto the doc.
    Call inside after_save of SewingInstructionStep or parent set.
    """
    source_text = step_doc.instruction_text_en or ""
    if not source_text.strip():
        return

    for lang in target_languages:
        if lang == "en":
            continue
        field = LANG_FIELD.get(lang)
        if not field:
            continue
        translated = translate_text(source_text, lang)
        frappe.db.set_value(
            "Sewing Instruction Step",
            step_doc.name,
            field,
            translated,
            update_modified=False,
        )


def translate_instruction_set_all(set_name: str) -> None:
    """
    Translate all steps in a SewingInstructionSet for all active languages.
    Called after save of the set doc.
    """
    try:
        from alice_shop_floor.alice_shop_floor.doctype.alice_settings.alice_settings import (
            get_settings, get_active_languages,
        )
        settings = get_settings()
        if not settings.translation_auto_on_save:
            return

        active_langs = [l["code"] for l in get_active_languages() if l["code"] != "en"]
        if not active_langs:
            return

        steps = frappe.get_all(
            "Sewing Instruction Step",
            filters={"parent": set_name},
            fields=["name", "instruction_text_en"],
            order_by="sequence asc",
        )
        if not steps:
            return

        for lang in active_langs:
            field = LANG_FIELD.get(lang)
            if not field:
                continue
            texts   = [s["instruction_text_en"] or "" for s in steps]
            xlated  = translate_batch(texts, lang)
            for i, step in enumerate(steps):
                frappe.db.set_value(
                    "Sewing Instruction Step", step["name"],
                    field, xlated[i],
                    update_modified=False,
                )

        frappe.db.commit()

    except Exception as exc:
        frappe.log_error(str(exc), "ALICE Translate Instruction Set")


# ── Operator language lookup ──────────────────────────────────────────────────

def get_operator_language(operator_user: str) -> str:
    """
    Return the preferred_language code for an operator (frappe user or employee).
    Falls back to 'en'.
    """
    if not operator_user:
        return "en"
    # Try Employee record linked to user
    emp = frappe.db.get_value(
        "Employee",
        {"user_id": operator_user},
        "preferred_language",
    )
    if emp:
        return emp
    # Fallback: check if operator_user is already an employee name
    emp2 = frappe.db.get_value(
        "Employee",
        {"name": operator_user},
        "preferred_language",
    )
    return emp2 or "en"


# ── Step text resolver (language-aware) ──────────────────────────────────────

def get_step_text(step: dict, language: str) -> str:
    """
    Return the instruction text for *step* in *language*.
    step is a dict with keys like instruction_text_en, instruction_text_es, etc.
    Falls back to English if the requested language is empty.
    """
    field   = LANG_FIELD.get(language, "instruction_text_en")
    text    = step.get(field, "") or ""
    if not text.strip() and language != "en":
        text = step.get("instruction_text_en", "") or ""
    return text
