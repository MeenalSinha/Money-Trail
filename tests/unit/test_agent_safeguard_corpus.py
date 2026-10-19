"""Measured behaviour of the grounding filter on a labelled corpus.

IN-SCOPE adversarial statements (fabricated identifiers, invented/converted numbers, wrong timestamps, unhedged crime
conclusions) must ALL be rejected. GROUNDED statements should survive (false rejections are counted, and bounded).
OUT-OF-SCOPE statements are purely semantic fabrications with no identifier, number, timestamp or configured term; the filter
cannot detect them. They are listed so the limitation is measured and documented, not hidden: they pass today, and the
primary protection against them is structural (the model cannot create findings or change classifications)."""
import pytest

from test_agent_grounding_report import checker, detection, ledger_with

T1, T2, T3 = "ns:T1", "ns:T2", "ns:T3"

ADVERSARIAL = {
    "fabricated_transaction_id": [f"Transaction ns:T777 also belongs to this cycle.", f"The cycle includes {T1} and ns:T404.",
                                  "See ns:TXN000099999 for the return leg.", f"{T3} was preceded by ns:T55."],
    "fabricated_account_id": ["Funds ended at ns:ZZ9.", "Account ns:SHELL1 received the proceeds.", "The hub account ns:HUB_X is central.",
                              "ns:A and ns:Q99 appear together."],
    "fabricated_finding_evidence_id": ["Finding fnd:deadbeefdeadbeefdeadbeefdeadbeef confirms it.", "Evidence evd:00000000000000000000000000000000 supports this.",
                                       "Investigation inv:11111111111111111111111111111111 closed it."],
    "invented_amount": ["The first transfer moved 250000 USD.", "About 999999 USD circulated in total.", "A fee of 7500 was skimmed at each hop.",
                        "The total value was 330000 USD."],
    "model_arithmetic_or_conversion": ["The first transfer was 1,109.82 USD.", "Roughly 98.9 percent was retained per hop.",
                                       "The amount fell by 1110 between hops.", "Total moved was 329627 USD."],
    "wrong_timestamp": ["The first transfer happened on 2026-03-03.", "It was booked at 2026-01-04T12:00:00Z.", "Activity began 2025-12-31."],
    "unhedged_conclusion": ["This is money laundering.", "The owners are criminals.", "This is clearly fraud.", "The ring is guilty of layering.",
                            "These accounts launder funds."],
    "mixed": [f"{T1} proves fraud by ns:OWNER7 for 500000 USD.", f"Because of {T2}, 12345 USD was laundered."],
    "bare_identifier_like_token": ["See TXN123456 for details.", "Case AB123456 is related.", "Reference CASE20260101 was opened."],
}
GROUNDED = [f"{T1} is part of a circular pattern.", f"The pattern includes {T1}, {T2} and {T3}.", f"{T1} sent 110982 USD.",
            "The cycle spans 2000 ms across 3 accounts.", "The first transfer was booked on 2026-01-03.", "This may warrant review.",
            "The structure does not establish fraud.", "Funds may have returned to ns:A, which could warrant a closer look.",
            "No wrongdoing is established by this pattern.", "The observed window is short relative to the configured limit.",
            "Three transactions form the cycle.", "Review of the supporting records is suggested if further context is available.",
            f"{T2} moved 109872 USD from ns:B to ns:C.", "The pattern has 3 transactions and 3 accounts.", f"ns:A, ns:B and ns:C each appear once.",
            "A hypothesis, unconfirmed, is that the accounts are related."]
OUT_OF_SCOPE = ["The account holders clearly knew each other.", "This looks like a coordinated effort by a single organisation.",
                "The money was moved to avoid detection.", "One million dollars changed hands.",
                "The funds came from an overseas source."]


def keeps(text):
    L, _ = ledger_with()
    return bool(checker(L).check(text).text)


def test_every_in_scope_adversarial_statement_is_rejected():
    results = {cat: [keeps(s) for s in items] for cat, items in ADVERSARIAL.items()}
    leaked = [(cat, s) for cat, items in ADVERSARIAL.items() for s, k in zip(items, results[cat]) if k]
    total = sum(len(v) for v in ADVERSARIAL.values())
    print(f"\nADVERSARIAL in-scope: {total - len(leaked)}/{total} rejected; leaked: {leaked}")
    assert leaked == []


def test_grounded_statements_survive_with_bounded_false_rejection():
    rejected = [s for s in GROUNDED if not keeps(s)]
    print(f"\nGROUNDED: {len(GROUNDED) - len(rejected)}/{len(GROUNDED)} kept; falsely rejected: {rejected}")
    assert len(rejected) / len(GROUNDED) <= 0.10, rejected


def test_out_of_scope_semantic_fabrications_are_a_documented_limitation():
    passed = [s for s in OUT_OF_SCOPE if keeps(s)]
    print(f"\nOUT-OF-SCOPE semantic statements passing the filter: {len(passed)}/{len(OUT_OF_SCOPE)} (documented limitation)")
    assert passed == OUT_OF_SCOPE          # if this ever changes, update docs/agent.md#limitations
