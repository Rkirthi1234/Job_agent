"""Monster dynamic application-question handling (Playwright side).

Follows the Wellfound pattern (wellfound_questions.py + the scan/fill code
inside wellfound.py), kept in one module for Monster:

  scan   -- read the visible, non-standard questions out of the DOM
            (text, textarea, select, radio groups, checkbox groups)
  answer -- ask MonsterQuestionAnswerer, which IS the Wellfound answerer:
            candidate profile / custom_qa_memory first, deterministic
            work-authorization / sponsorship / link handling, then the
            LLM strictly grounded in stored candidate + job data
  fill   -- put the answer into the matching control
  verify -- read the control back; a click or fill is never proof

NEVER INVENTS AN ANSWER. A question is only filled when the answerer
returned a confident answer that could be written into the page AND read
back. Otherwise:
  - optional question  -> skipped
  - required question  -> reported in QuestionRunResult.required_unanswered,
                          which the adapter turns into
                          status="manual_review",
                          blocker="required_question_unanswered".

Two extra guards that the shared answerer does not have:
  - consent-style controls ("I agree", terms, privacy, certifications) are
    NEVER ticked automatically -- agreeing on someone's behalf is not
    something a profile can authorise;
  - sensitive/demographic questions (gender, race, veteran status,
    disability, ...) are only answered from an explicit profile or
    custom_qa_memory match, never from an LLM guess.

The DOM scan does not depend on generated CSS classes: labels come from
aria-labelledby / label[for] / wrapping label / aria-label / fieldset
legend, and "required" from the required attribute, aria-required or an
asterisk in the label. Only the fields MonsterApplicationSource fills
deterministically itself (name, email, phone, resume) are excluded; a
location/company/link/experience field the adapter could not fill (no
stored data) is picked up here, so a required one is reported instead of
silently left empty. The scan is limited to the application container the
adapter tagged (data-monster-apply-scope), so the site header / search box
is never treated as a question.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.integrations.application_sources.playwright_support import FillOutcome
from app.integrations.application_sources.wellfound_questions import WellfoundQuestionAnswerer
from app.schemas.application import ApplicationPayload

logger = logging.getLogger(__name__)

#: Below this confidence an answer is treated as "no answer".
MIN_CONFIDENCE = 0.5

#: Consent-style controls are never ticked/answered automatically.
_NEVER_AUTO_ANSWER_RE = re.compile(
    r"\b(i agree|i accept|i consent|i acknowledge|i certify|i confirm that|terms (?:of|and)|"
    r"privacy (?:policy|notice)|consent to|agree to (?:the|our)|acknowledg|certify that|"
    r"electronic signature|e-?sign)",
    re.IGNORECASE,
)

#: Demographic / legally sensitive questions: profile or Q&A memory only.
_SENSITIVE_RE = re.compile(
    r"\b(gender|sex\b|race|ethnic|hispanic|latino|veteran|disabilit|sexual orientation|"
    r"religio|pronoun|transgender|marital|date of birth|birth date|age\b|criminal|convicted|"
    r"citizenship|national origin)",
    re.IGNORECASE,
)


class MonsterQuestionAnswerer(WellfoundQuestionAnswerer):
    """Same grounded behaviour as the Wellfound answerer (profile and
    custom_qa_memory first, deterministic sponsorship/authorization/link
    answers, then an LLM that may only use stored candidate + job data).
    Kept as its own class so Monster can diverge without touching
    Wellfound."""


@dataclass
class QuestionRunResult:
    detected: int = 0
    answered: int = 0
    skipped: int = 0
    required_unanswered: list[str] = field(default_factory=list)


# Runs in the page, returns a JSON-serialisable list of question dicts.
# Reads labels / options / flags only -- never a field's value.
SCAN_JS = r"""
() => {
  const questions = [];
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const root = document.querySelector('[data-monster-apply-scope="1"]') || document;
  const isVisible = el => {
    if (!el) return false;
    try {
      const r = el.getBoundingClientRect();
      const s = window.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    } catch (e) { return false; }
  };
  const byIds = ids => (ids || '').split(/\s+/).map(id => {
    const n = document.getElementById(id); return n ? n.innerText : '';
  }).join(' ');
  const labelFor = el => {
    let t = '';
    const lb = el.getAttribute('aria-labelledby');
    if (lb) t = byIds(lb);
    if (!clean(t) && el.id) {
      const l = Array.from(root.querySelectorAll('label')).find(x => x.htmlFor === el.id);
      if (l) t = l.innerText;
    }
    if (!clean(t)) { const w = el.closest('label'); if (w) t = w.innerText; }
    if (!clean(t)) t = el.getAttribute('aria-label') || '';
    return clean(t);
  };
  const groupLabel = (inputs) => {
    const first = inputs[0];
    const grp = first.closest('fieldset, [role="radiogroup"], [role="group"]');
    let t = '';
    if (grp) {
      const lb = grp.getAttribute('aria-labelledby');
      if (lb) t = byIds(lb);
      if (!clean(t)) { const lg = grp.querySelector('legend'); if (lg) t = lg.innerText; }
      if (!clean(t)) t = grp.getAttribute('aria-label') || '';
      if (!clean(t)) { const prev = grp.previousElementSibling; if (prev) t = prev.innerText; }
    }
    if (!clean(t)) {
      const box = first.closest('div, li, section');
      const head = box ? box.querySelector('legend, h3, h4, p, span') : null;
      if (head && !head.contains(first)) t = head.innerText;
    }
    return clean(t);
  };
  // Fields the adapter fills itself, deterministically.
  const OWN_FIELD = new RegExp(
    '^(first|last|full|middle|preferred|legal)?\\s*name$|^e-?mail( address)?$|' +
    '^(mobile|cell|home|primary)?\\s*(phone|telephone|mobile|cell)( number)?$|' +
    'resume|^cv$|curriculum', 'i');
  const isOwnField = label => OWN_FIELD.test(clean(label).replace(/[*:]/g, '').trim());
  const requiredFlag = (els, label) =>
    els.some(e => e.required || e.hasAttribute('required') || e.getAttribute('aria-required') === 'true') ||
    /\*\s*$/.test(label) || /\(required\)/i.test(label);

  // radio / checkbox groups, grouped by type + name
  const groups = new Map();
  for (const el of root.querySelectorAll('input[type="radio"], input[type="checkbox"]')) {
    if (!isVisible(el) && !isVisible(el.closest('label'))) continue;
    const key = el.type + '::' + (el.name || el.id || Math.random());
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(el);
  }
  for (const [key, inputs] of groups) {
    const type = inputs[0].type;
    let label = groupLabel(inputs);
    if (inputs.length === 1 && !label) label = labelFor(inputs[0]);
    if (!label || isOwnField(label)) continue;
    const optionsMeta = inputs.map(i => ({label: labelFor(i) || i.value || '', id: i.id || '', value: i.value || ''}));
    const options = optionsMeta.map(o => o.label).filter(Boolean);
    const qId = inputs[0].name || inputs[0].id || label.slice(0, 30).replace(/\W+/g, '_').toLowerCase();
    questions.push({
      question_id: qId, label: label, field_type: type,
      required: requiredFlag(inputs, label), options: options, options_meta: optionsMeta,
      input_name: inputs[0].name || '', has_existing_value: inputs.some(i => i.checked),
    });
  }

  // text-like inputs, textareas, selects
  const sel = 'input:not([type="radio"]):not([type="checkbox"]):not([type="hidden"]):not([type="file"])' +
              ':not([type="submit"]):not([type="button"]):not([type="password"]):not([type="search"])' +
              ':not([type="image"]):not([type="reset"]), textarea, select';
  for (const el of root.querySelectorAll(sel)) {
    if (!isVisible(el)) continue;
    const tag = el.tagName.toLowerCase();
    const label = labelFor(el);
    if (!label || isOwnField(label)) continue;
    const ftype = tag === 'textarea' ? 'textarea' : (tag === 'select' ? 'select' : 'text');
    let options = [];
    if (tag === 'select') {
      options = Array.from(el.querySelectorAll('option')).map(o => clean(o.innerText))
        .filter(t => t && !/^(select|choose|please select|--)/i.test(t));
    }
    const qId = el.id || el.name || label.slice(0, 30).replace(/\W+/g, '_').toLowerCase();
    if (!qId || questions.some(q => q.question_id === qId)) continue;
    questions.push({
      question_id: qId, label: label, field_type: ftype,
      required: requiredFlag([el], label), options: options,
      has_existing_value: !!(el.value && el.value.trim()),
    });
  }
  return questions;
}
"""


def css_attr_value(value: str) -> str:
    """Escape a value for use inside a double-quoted CSS attribute selector."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def id_or_name_selector(question_id: str) -> str:
    esc = css_attr_value(question_id)
    return f'[id="{esc}"], input[name="{esc}"], textarea[name="{esc}"], select[name="{esc}"]'


class MonsterQuestionHandler:
    """Scans, answers, fills and verifies a Monster form's dynamic questions."""

    def __init__(self, answerer: WellfoundQuestionAnswerer | None = None) -> None:
        self._answerer = answerer

    def _get_answerer(self) -> WellfoundQuestionAnswerer:
        if self._answerer is None:
            self._answerer = MonsterQuestionAnswerer()
        return self._answerer

    # -- scan ------------------------------------------------------------

    def scan(self, page) -> list[dict[str, Any]]:
        try:
            result = page.evaluate(SCAN_JS)
        except Exception as exc:
            logger.info("Monster question scan failed/skipped: %s", exc)
            return []
        if not isinstance(result, list):
            return []
        return [q for q in result if isinstance(q, dict) and (q.get("label") or q.get("question_id"))]

    # -- orchestration ----------------------------------------------------

    def run(self, page, payload: ApplicationPayload, outcome: FillOutcome) -> QuestionRunResult:
        questions = self.scan(page)
        result = QuestionRunResult(detected=len(questions))
        outcome.mark("dynamic_questions_detected", str(result.detected))

        for question in questions:
            q_id = self._audit_id(question)
            label = (question.get("label") or "").strip()
            required = bool(question.get("required"))
            outcome.mark(f"question_{q_id}_type", str(question.get("field_type", "text")))

            if question.get("has_existing_value"):
                outcome.mark(f"question_{q_id}", "prefilled")
                continue

            answer, source = self.decide(payload, question)
            if answer is not None:
                if self.fill(page, question, answer):
                    # Allow React/Angular state to settle before reading the
                    # control back: a radio click that lands correctly can still
                    # read as unchecked if verify() runs synchronously on the
                    # same tick as the click (seen with Monster's Work Authorization).
                    try:
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                    if self.verify(page, question, answer):
                        result.answered += 1
                        outcome.mark(f"question_{q_id}", "answered")
                        outcome.mark(f"question_{q_id}_source", str(source))
                        continue
                logger.warning("Monster: answer for question %r could not be written and verified", label)

            if not required:
                result.skipped += 1
                outcome.mark(f"question_{q_id}", "skipped")
                continue

            result.required_unanswered.append(label or q_id)
            outcome.mark(f"question_{q_id}", "unanswered_required")
            outcome.mark(f"question_{q_id}_source", str(source))

        outcome.mark("dynamic_questions_answered", str(result.answered))
        outcome.mark("dynamic_questions_skipped", str(result.skipped))
        outcome.mark("dynamic_questions_required_unanswered", str(len(result.required_unanswered)))
        return result

    @staticmethod
    def _audit_id(question: dict[str, Any]) -> str:
        raw = str(question.get("question_id") or question.get("label") or "unnamed")
        return re.sub(r"[^A-Za-z0-9_-]+", "_", raw)[:40] or "unnamed"

    def decide(self, payload: ApplicationPayload, question: dict[str, Any]) -> tuple[Any, str]:
        """Return (answer or None, source). None means "do not fill"."""
        label = (question.get("label") or "").strip()
        if label and _NEVER_AUTO_ANSWER_RE.search(label):
            return None, "consent_requires_human"

        response = self._get_answerer().answer_question(payload.candidate, payload.job, question)
        answer = response.get("answer")
        source = str(response.get("source") or "not_available")
        try:
            confidence = float(response.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if answer is None:
            return None, source
        if label and _SENSITIVE_RE.search(label) and not (source == "candidate_profile" and confidence >= 1.0):
            return None, "sensitive_not_in_profile"
        if confidence < MIN_CONFIDENCE:
            return None, "low_confidence"
        return answer, source

    # -- fill -------------------------------------------------------------

    def fill(self, page, question: dict[str, Any], answer: Any) -> bool:
        try:
            field_type = question.get("field_type")
            if field_type == "radio":
                return self._fill_radio(page, question, answer)
            if field_type == "checkbox":
                return self._fill_checkbox(page, question, answer)
            if field_type == "select":
                return self._fill_select(page, question, answer)
            if field_type in ("text", "textarea"):
                return self._fill_text(page, question, answer)
        except Exception:
            logger.exception("Monster: failed to fill question %r", question.get("label"))
        return False

    @staticmethod
    def _fill_text(page, question: dict[str, Any], answer: Any) -> bool:
        target = page.locator(id_or_name_selector(str(question.get("question_id", "")))).first
        if target.count() == 0:
            return False
        target.fill(str(answer), timeout=3000)
        try:
            target.evaluate(
                "el => { el.dispatchEvent(new Event('input', {bubbles: true})); "
                "el.dispatchEvent(new Event('change', {bubbles: true})); }"
            )
        except Exception:
            pass
        return True

    @staticmethod
    def _fill_select(page, question: dict[str, Any], answer: Any) -> bool:
        target = page.locator(id_or_name_selector(str(question.get("question_id", "")))).first
        if target.count() == 0:
            return False
        target.select_option(label=str(answer).strip())
        return True

    @staticmethod
    def _match_meta(question: dict[str, Any], answer: Any) -> dict[str, Any] | None:
        wanted = str(answer).strip().lower()
        metas = question.get("options_meta") or []
        for meta in metas:
            if (meta.get("label") or "").strip().lower() == wanted:
                return meta
        for meta in metas:
            label = (meta.get("label") or "").strip().lower()
            if label and (wanted in label or label in wanted):
                return meta
        return None

    @staticmethod
    def _locate_option(page, question: dict[str, Any], meta: dict[str, Any]):
        input_type = question.get("field_type", "radio")
        if meta.get("id"):
            candidate = page.locator(f'[id="{css_attr_value(meta["id"])}"]').first
            if candidate.count() > 0:
                return candidate
        name, value = question.get("input_name") or "", meta.get("value")
        if name and value:
            candidate = page.locator(
                f'input[type="{input_type}"][name="{css_attr_value(name)}"][value="{css_attr_value(value)}"]'
            ).first
            if candidate.count() > 0:
                return candidate
        return None

    @staticmethod
    def _click_input(locator) -> bool:
        try:
            locator.evaluate("el => el.click()")
            return True
        except Exception:
            try:
                locator.click()
                return True
            except Exception:
                return False

    def _fill_radio(self, page, question: dict[str, Any], answer: Any) -> bool:
        meta = self._match_meta(question, answer)
        if meta is None:
            return False
        target = self._locate_option(page, question, meta)
        return target is not None and self._click_input(target)

    def _fill_checkbox(self, page, question: dict[str, Any], answer: Any) -> bool:
        items = answer if isinstance(answer, list) else [answer]
        filled = False
        for item in items:
            meta = self._match_meta(question, item)
            if meta is None:
                return False
            target = self._locate_option(page, question, meta)
            if target is None:
                return False
            if self._is_checked(target) or self._click_input(target):
                filled = True
            else:
                return False
        return filled

    # -- verify -----------------------------------------------------------

    @staticmethod
    def _is_checked(locator) -> bool:
        try:
            if hasattr(locator, "is_checked"):
                return bool(locator.is_checked())
        except Exception:
            pass
        try:
            return bool(locator.evaluate("el => el.checked"))
        except Exception:
            return False

    def verify(self, page, question: dict[str, Any], expected: Any) -> bool:
        """Read the control back. Radios/checkboxes must actually be
        checked; text/textarea must hold the value; selects must hold a
        non-empty value."""
        field_type = question.get("field_type")
        try:
            if field_type in ("radio", "checkbox"):
                items = expected if isinstance(expected, list) else [expected]
                for item in items:
                    meta = self._match_meta(question, item)
                    target = self._locate_option(page, question, meta) if meta else None
                    if target is None or not self._is_checked(target):
                        return False
                return True
            target = page.locator(id_or_name_selector(str(question.get("question_id", "")))).first
            if target.count() == 0 or not hasattr(target, "input_value"):
                return False
            value = (target.input_value() or "").strip()
            if field_type == "select":
                return bool(value)
            return value == str(expected).strip()
        except Exception:
            return False
