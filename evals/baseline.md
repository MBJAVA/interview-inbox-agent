| Metric | Agent | One-shot baseline |
|---|---|---|
| classification_accuracy | 1.0 | 1.0 |
| extraction_accuracy | 1.0 | 0.739 |
| links_ok_rate | every link checked; broken in final docs: 0 | 0.451 |
| duplicate_docs | 0 | 5 |
| slot_compliance | 0.996 | 0.506 |
| quality_violations | 0 | 150 |

Baseline link statuses: {'unverified': 31, 'ok': 32, 'none': 8}. Model: offline policy model (MOCK_LLM).