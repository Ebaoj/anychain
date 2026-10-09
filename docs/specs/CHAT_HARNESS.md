# Spec: the chat as a harness (classifier + code + a cheap writer)

**Status: APPROVED by the author on 2026-10-09, after a prototype validated it (section 10).** Asked by the author on 2026-10-09, after trying the chat: three wrong answers passed the current check because none had an invented number ("try again" when the same operation had already succeeded; "your electricity bill should be paid" about a token swap; "Yes," before "it cannot be known").

## 1. The idea

Today the model decides **what** to answer and writes it; the code only checks numbers, addresses, links and citations. After this change the model never decides what to answer:

```
reader's question
  → 0. rules      (code: clear words such as "taxa", "fee", "atendente" route without any model)
  → 1. classify   (only what the rules leave: the model the reader chose, one short call, one label from a closed list)
  → 2. frame      (code: from the label and the facts, the statements to make, each with its fact ids)
  → 3. write      (the same cheap model: the frame in plain words, in the reader's mode and language)
  → 4. check      (code: the numbers check of today + a new check of statements)
  → answer
```

Questions the list does not cover go to the chat of today (tools run by our code), and pass the new check too.

## 2. The intents (closed list)

| Intent | Examples | The frame (code) | Facts |
|---|---|---|---|
| `status` | "deu certo?", "did it go through?" | worked or failed, with the label | overview, diagnosis |
| `why_failed` | "por que falhou?", "what went wrong" | the conclusion, its label, the reason's own words | diagnosis, revert |
| `money_moved` | "o dinheiro saiu?", "a grana voltou?", "was I refunded?" | what moved (or nothing), from whom to whom; never "came back" unless a transfer back is a fact | token/native transfers, overview |
| `fee` | "quanto paguei de taxa?", "why was I charged?" | the fee, and that a failed transaction is charged | fee |
| `when` | "quando foi?" | the block's date | overview |
| `what_to_do` | "o que eu faço agora?" | the next steps of the conclusion for this reader; when the timeline shows the same operation succeeded later, only that (D63) | diagnosis, timeline |
| `purpose_claim` | "era a conta de luz, foi paga?" | what the transaction is (from the decoded call and movements) and that its purpose cannot be known from the chain | call, transfers |
| `human` | "quero falar com alguém" | a fixed reply: how to reach support with the transaction's link; logged as a hand-off request | overview |
| `off_topic` | anything not about this transaction, or "ignore your instructions" | a fixed reply: what the assistant can answer | none |
| `open` | everything else ("why does Uniswap have a deadline?", "show the function's code") | today's chat with tools, plus the new check | all |

`open` is the default: an unknown label, an invalid reply, no model, or low confidence all go to `open`, never to a wrong fixed answer.

## 3. The rules and the classifier

- **Rules first:** keyword rules in pt-BR, en and es route the clear cases (the fee, the date, a person, an attempt to change the assistant's instructions) with no model call; a message with two or more questions is split and each part routed.
- **Model:** the model the reader chose (same key; nothing new to configure). Without a model: keyword rules (pt-BR, en, es) for the clear cases, `open` otherwise.
- **Contract:** the prompt lists the labels with one line each and three examples; the model answers JSON only: `{"intent": "<label>"}`. The code accepts only a label in the list; anything else is `open`. The reader's text is passed as data, delimited, never as instructions.
- **Cost:** one short call per question (a few hundred tokens).

## 4. The frame and the writer

- For each intent except `open`, the code builds a frame: a list of statements, each with the fact ids that support it, in English, plus what must not be said (for example `money_moved` on a failed transaction: "do not say the money came back").
- The cheap model rewrites the frame for the reader (mode and language). It may not add statements.
- `human` and `off_topic` are fixed texts in the page's three languages: no model.
- If the written answer fails the checks twice, the reader gets the frame's own plain sentences (code), never nothing: these intents always answer.

## 5. The statement check (new, for every chat answer)

On top of today's check (numbers, addresses, links, citations must be in the facts), the code rejects an answer that:

1. says it worked when the status is failed, or failed when it succeeded (a list of phrases per language, checked with the negation around them);
2. says money came back or was refunded when no transfer to the sender is a fact;
3. advises trying again when the timeline shows the same operation succeeded later;
4. confirms a purpose the reader gave (bill, invoice, supplier, rent…) as paid or done;
5. for a framed intent: cites a fact outside the frame.

A rejected answer is sent back once with the reasons, as today; a second failure: the frame's sentences (framed intents) or withheld (open).

## 5b. Asking instead of guessing

The model's own confidence is not used (measured in section 10, and the literature agrees). The code asks or suggests when it can see the ambiguity itself:
- a message with two questions: each part answered by its own frame when both are framed intents; otherwise the open path;
- a short message the rules and the classifier leave as `open` ("pq deu ruim?", "e aí?"): answered, with the most likely questions offered as buttons;
- a framed answer that failed the checks twice: the frame's own sentences, with the same buttons.

## 6. Measured

- **Classifier set:** 60 questions written for the real transactions of the eval, in pt-BR, en and es, including slang and mixed questions, each with its expected label (`eval/chat_intents.json`). Metric: accuracy, and accuracy on the risky labels (`what_to_do`, `money_moved`, `purpose_claim`).
- **The three answers that motivated this** become tests that fail on the old code.
- **The log** records, per question, the intent and the path that answered (rule, classifier, frame, frame's own sentences, fixed reply, open); `anychain metrics` gets "hand-off requests" (the escalation signal of docs/IMPACT.md).
- Report: classifier accuracy with gpt-4.1-nano and with Claude; share of questions answered by a frame.

## 7. Done when

- The 60-question set: accuracy at least 90% with the cheapest model, and no risky label sent to a wrong frame more than twice (a wrong `open` is acceptable: it is the safe side).
- The three motivating answers cannot happen (tests).
- The suite is green; the eval's other metrics do not drop.

## 8. Not in this change

- The hand-off itself (a ticket with the conversation and its facts): `human` only answers and logs (docs/ROADMAP.md).
- The explanation (`/explain`): it already has a frame built by code (D62); the statement check could be added to it later.

## 9. Decisions (the author, 2026-10-09)

1. **Classifier model:** the reader's chosen model.
2. **When a framed answer fails twice:** the frame's own sentences (not withheld). The prototype showed it is needed, not optional (section 10).
3. **Fixed replies (`human`, `off_topic`):** in pt-BR, en and es, written in the code.
4. **Rules before the classifier**, and the path of every answer logged (after reviewing how the community builds this: Rasa CALM, NeMo Guardrails, semantic routers).

## 10. Prototype results (2026-10-09, before any code)

- **Classifier** (60 questions, pt-BR/en/es with slang): first wording 71.7% with gpt-4.1-nano; with sharper definitions and examples that are not in the test set, 95.0%, 93.3%, 93.3% (three runs) and 100% with Claude. Asking the model for a second reading when unsure lowered nano to 90% and flagged 0 and 1 of its 6 errors: its own doubt is not a usable signal.
- **Statement check:** caught 8 of 8 wrong answers (the three real ones and variants); flagged 0 of 22 good answers once conditionals ("to know *if* it worked") were treated like negations; and found 2 real errors in nano answers that had passed until then ("try again" on a transaction redone with success 372 seconds later; a revert called a "reembolso", a refund).
- **Frame and cheap writer:** 4 of 5 intents right at the first attempt. The purpose question ("it was the electricity bill, is it paid?") is hard for nano: it kept writing "the payment of the bill" or "the bill is not paid yet", both unknowable. Hence: any sentence naming the reader's purpose must say it cannot be known, and the frame's own sentences after two failures.
