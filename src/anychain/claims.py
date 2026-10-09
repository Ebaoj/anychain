"""The statement check (D74, docs/specs/CHAT_HARNESS.md section 5): wrong claims that carry no number.

The value check (validator.py) rejects a number, address, link or citation that is not in the facts. These rules
reject what it cannot see, each from a real answer of 2026-10-09:
  - it worked, said of a failed transaction (or the reverse);
  - the money came back or was refunded, with no transfer back to the sender among the facts;
  - try again, when the sender's timeline shows the same operation succeeded later (D63);
  - a purpose the reader gave (a bill, the rent, a supplier) spoken of as known, paid or not: the chain cannot say.

A phrase inside a negation or a condition ("did not work", "to know if it worked") is not a claim. Sentences about
the later attempt ("the next one succeeded") are not a claim about this transaction. Validated before the code on
8 wrong answers (all caught) and 22 good ones (none flagged): section 10 of the spec.
"""
import re

from anychain.models import EvidenceBundle

# Words that, before a phrase in the same clause, make it a negation or a condition: "não deu certo", "se deu certo".
# The clause ends at , ; : and "mas/but/pero", so "Não se preocupe: o dinheiro voltou" is still a claim.
NEGATION = (r"(?:\b(?:não|nao|nunca|sem|not|never|no|nothing|nada|nenhum|nenhuma|ningún|ninguna|tampoco|se|if|"
            r"whether|si)\b|n't\b)")
CLAUSE_END = r"[,;:.!?]|\b(?:mas|porém|but|pero|however|embora)\b"
SUCCESS = [r"deu certo", r"foi conclu[íi]d[ao] com sucesso", r"foi bem[- ]sucedid[ao]", r"foi aprovad[ao]",
           r"foi confirmad[ao] com sucesso", r"foi processad[ao] com sucesso", r"\bsucceeded\b", r"was successful",
           r"went through", r"\bworked\b", r"se complet[óo]", r"fue exitos[ao]", r"funcion[óo]"]
FAILED = [r"\bfalhou\b", r"não deu certo", r"\bfailed\b", r"was reverted", r"foi revertid[ao]", r"\bfall[óo]\b"]
REFUND = [r"\bvoltou\b", r"devolvid[ao]", r"reembols", r"estornad", r"refund", r"came back", r"returned to you",
          r"sent back", r"retornou", r"devuelt[ao]", r"\bdevolvi", r"regres[óo]"]
RETRY = [r"tent(?:e|ar|a|em) (?:novamente|de novo|outra vez)", r"try(?:ing)? (?:it )?again", r"\bretry\b",
         r"\breenvi", r"envi(?:e|ar|a) (?:de novo|novamente|outra vez)", r"(?:submit|send) (?:it )?again", r"\bresend",
         r"intent(?:e|ar|a) (?:de nuevo|otra vez|nuevamente)", r"vuelv[ae] a intentar",
         r"refa[çz](?:a|er) a (?:operação|transação)"]
# A sentence about another attempt or another call is not about this transaction ("the same operation succeeded
# later", "an internal call failed"). Only these phrases, never "depois" or "next" alone.
OTHER = (r"mesm[ao] (?:operação|troca|transferência|chamada|pedido)|same (?:operation|call|swap|transfer)|"
         r"(?:tentativa|transação|operação) (?:seguinte|posterior|anterior)|(?:next|later|following|subsequent|"
         r"earlier|previous) (?:attempt|transaction|try)|nonce \d+|outra (?:tentativa|transação)|another "
         r"(?:attempt|transaction)|misma operación|siguiente (?:intento|transacción)|chamada interna|internal call|"
         r"llamada interna|depois d(?:essa|esta|a) falha|after (?:this|the) failure|después de (?:este|ese) fallo|"
         r"primeira vez que|first time (?:it|that)|endpoint|\brpc\b|explorador|explorer|\bo nó\b|the node|"
         r"\bfonte\b|\bsource\b|servidor|\bserver\b")
UNKNOWABLE = (r"não (?:dá|é possível|podemos|temos como|há como) (?:para )?(?:saber|afirmar|confirmar|dizer)|"
              r"não sabemos|não (?:aparece|mostra|indica)|cannot (?:be )?(?:know|tell|confirm|say)|can't (?:be )?"
              r"(?:know|tell|confirm|say)|(?:is|are) not (?:known|shown)|does not (?:show|say)|no (?:se )?puede "
              r"(?:saber|confirmar|afirmar)|no (?:muestra|aparece)")
YES = r"^\W*(?:sim|yes|sí|si|claro|certo|correto|exato|of course|sure)\b"
# What a reader says a payment was for: never confirmed nor denied (only the chain's facts are).
# ("conta" alone is not one: "sua conta" is also the reader's wallet.)
PURPOSES = ["conta de luz", "conta de água", "conta de agua", "conta de gás", "conta de telefone", "conta de internet",
            "boleto", "fatura", "aluguel", "fornecedor", "mensalidade", "salário", "salario", "imposto", "bill",
            "invoice", "rent", "supplier", "vendor", "salary", "tuition", "factura", "alquiler", "proveedor", "cuota"]


def purposes_in(text: str | None) -> list[str]:
    """The purposes a reader's message names ("era a conta de luz"); the longest phrase wins over its words."""
    low = (text or "").lower()
    found = [p for p in PURPOSES if re.search(rf"\b{re.escape(p)}\b", low)]
    return [p for p in found if not any(p != q and p in q for q in found)]


def _sentences(text: str):
    for s in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        s = s.strip(" *-#")
        if s:
            yield s


def _clauses(sentence: str) -> list[str]:
    return [c for c in re.split(CLAUSE_END, sentence) if c and c.strip()]


def _claims(patterns: list[str], clause: str) -> str | None:
    """The first pattern stated in the clause, with no negation or condition before it in the same clause."""
    for p in patterns:
        for m in re.finditer(p, clause):
            if not re.search(NEGATION, clause[:m.start()]):
                return p
    return None


def facts_of(bundle: EvidenceBundle) -> dict:
    """What the rules need to know about the transaction, from its facts only."""
    refund_fact = any(re.search(r"refund|reembols|devolv|return", e.text, re.IGNORECASE) for e in bundle.items
                      if e.kind not in ("code", "source", "security_note"))
    later = [e.id for e in bundle.items if e.kind == "timeline"
             and e.data.get("pattern") in ("retried_ok", "approved_then_ok")]
    return {"status": bundle.status, "refund_fact": refund_fact, "succeeded_later": later}


def check_claims(answer: str, bundle: EvidenceBundle, purposes: list[str] | None = None) -> list[str]:
    """Problems in `answer` the value check cannot see (empty: none found)."""
    facts, problems = facts_of(bundle), []
    status = facts["status"]
    low_answer = (answer or "").lower()
    if purposes and any(re.search(rf"\b{re.escape(p)}\b", low_answer) for p in purposes) and re.search(YES, low_answer):
        problems.append("it opens with a yes to a question about what the reader said the payment was for: the facts "
                        "cannot confirm it")
    for sentence in _sentences(answer):
        low = sentence.lower()
        about_other = re.search(OTHER, low) is not None  # the sentence is about another attempt or call
        for clause in _clauses(low):
            if status != "success" and not about_other and _claims(SUCCESS, clause):
                problems.append(f"it says the transaction worked, but it {'failed' if status == 'failed' else 'is ' + str(status)}: "
                                f"'{sentence[:120]}'")
            if status != "failed" and not about_other and _claims(FAILED, clause):
                problems.append(f"it says the transaction failed, but it {'succeeded' if status == 'success' else 'is ' + str(status)}: "
                                f"'{sentence[:120]}'")
            if not facts["refund_fact"] and _claims(REFUND, clause):
                problems.append(f"it says money came back or was refunded, but no fact says so: '{sentence[:120]}' "
                                "(a revert is not a refund)")
            if facts["succeeded_later"] and _claims(RETRY, clause):
                problems.append(f"it advises trying again, but the same operation already succeeded later "
                                f"[{facts['succeeded_later'][0]}]: '{sentence[:120]}'")
            for purpose in purposes or []:
                named = rf"\b{re.escape(purpose)}\b"
                conditional = re.search(rf"\b(?:se|if|si|caso|in case)\b.*{named}", clause)  # "if the bill was this"
                if re.search(named, clause) and not conditional and not re.search(UNKNOWABLE, clause):
                    problems.append(f"it speaks of what the reader said the payment was for ('{purpose}') as known; "
                                    f"the facts cannot show it, so say only that: '{sentence[:120]}'")
    return list(dict.fromkeys(problems))
