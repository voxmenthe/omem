# Evaluating OMem

The existing tests establish engineering contracts, not improved agent decisions.
`orientation_trials.py` replays supplied selector fixtures. `semantic_eval.py`
scores a prepared legacy-style answer. Lifecycle tests supply the desired
consolidation judgment. None measures whether a model makes a good new judgment
or follows the delivered memory correctly.

Use three separate measurements for the next small evaluation:

| Stage | Input and control | Score |
| --- | --- | --- |
| Capture and judgment | About 12 real incidents, labeled before generating answers; keep expected judgments out of the model input | Missed capture, unsupported capture, correct integration/defer/reject/retire decisions, invented applicability, preservation of rare safeguards |
| Delivery | Freeze the resulting generations; another author writes task prompts and unrelated controls | Intended-item delivery, wrong-scope distractions, abstention, byte-budget losses, latency |
| Correct use and benefit | Matched tasks with the same model, tools, code and guidance; vary only OMem context and hold native memory fixed | Observable decision changes, repeated errors, successful task outcomes, unnecessary actions and total overhead |

Include a sparse new store, a strong correction, repeated wording from one
incident, independently recurring evidence, unrelated lessons competing for
space, a full catalog, a saturated deferred backlog with resolving evidence,
and an older claim that should be superseded. Test realistic paraphrases and
both personal/repository scope competition. Freeze model/version and record the
exact supplied evidence and returned result before scoring.

For each matched task, specify the relevant decision and success check before
running either condition. Counterbalance run order and have a reviewer who did
not write the answer score outcomes without seeing the condition label. Score
"retrieved" separately from "used correctly." Do not count a lesson as useful
merely because it appeared in context, and report inconclusive pairs.

Start with a small manually reviewed sample. No background collector, embedding
service or telemetry framework is needed. Use disposable memory roots and
preserve canonical evidence; keep private source sessions and live memory
contents outside the repository. Expand only to settle a concrete design choice.

The September 19 repair work included a private live-catalog diagnostic sample:
eight intended lessons reached the hook, and two unrelated prompts abstained.
The implementer also wrote the judgments and probes, so this is a self-check,
not an independent judgment-quality or task-benefit study. Historical plans and
earlier evaluation counts describe their dated revisions, not the current suite.
