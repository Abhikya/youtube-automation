"""Shared editorial guardrails, independent of channel registration or secrets."""
import re
import unicodedata


GLOBAL_CONTENT_GUARDRAILS = (
    ("religion / religious doctrine", r"religions?|religious|doctrines?|theology|worship|faith|christianity|christians?|islam|muslims?|hinduism|hindus?|buddhism|buddhists?|judaism|jewish|quran|bible"),
    ("gods / deities", r"god|gods|goddess|goddesses|deity|deities|zeus|odin|shiva|vishnu|allah"),
    ("religious figures", r"jesus|christ|buddha|messiah|prophet|saint|saints|pope|guru|krishna"),
    ("miracle-as-fact framing", r"miracle|miracles|miraculously|divine intervention"),
    ("caste disputes", r"caste[ -]+(?:based[ -]+)?(?:disputes?|conflicts?|violence|discrimination|hierarchy|system)|casteism"),
    ("communal conflict", r"communal[ -]+(?:conflicts?|violence|riots?|clashes|tensions?|disputes?|hatred)"),
    ("sectarian conflict", r"sectarian[ -]+(?:conflicts?|violence|riots?|clashes|tensions?|disputes?|hatred|divides?|division)"),
    ("inflammatory identity controversy", r"racial supremacy|white supremacy|ethnic cleansing|racial hatred|identity[ -]based (?:hatred|violence)|race war|hate speech"),
)
GLOBAL_GUARDRAIL_INSTRUCTIONS = (
    "Global editorial policy applies regardless of channel context: do not select or introduce "
    + "; ".join(category for category, _ in GLOBAL_CONTENT_GUARDRAILS)
    + ". Choose a different, factual topic within this channel's niche. Do not disguise excluded themes "
      "or substitute religion-adjacent content. Avoid miracle claims, including speculative miracle framing."
)


class ContentGuardrailError(ValueError):
    """Safe category-only rejection; normal generation retries handle it."""


def validate_content(*texts):
    """Conservative whole-word/phrase screening plus shared prompt instructions.

    Lexical screening is not a complete semantic classifier. Missing/empty content
    remains subject to the existing generators' and queue's structural validation.
    """
    for text in texts:
        if not isinstance(text, str):
            raise ContentGuardrailError("Content guardrail requires text")
        normalized = unicodedata.normalize("NFKC", text).casefold()
        # Established physics terminology, not theological content.
        normalized = re.sub(r"\bgod[ -]particle\b", "higgs boson", normalized)
        for category, expression in GLOBAL_CONTENT_GUARDRAILS:
            if re.search(r"\b(?:" + expression + r")\b", normalized):
                raise ContentGuardrailError("Global content guardrail rejected: " + category)
