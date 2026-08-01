# Template — Runtime Agent Definition

Copy this file to `.claude/agents/<kebab-name>.md`. The filename is the agent's identity across the event bus, the memory namespaces, and the audit trail, so pick it once and do not rename it. Example: `.claude/agents/risk-manager.md`.

This defines an LLM agent the system runs unattended. Everything below the frontmatter is the agent's prompt: it will be read by a model with no memory of this conversation, no access to your reasoning, and a strong prior toward being agreeable. Write for that reader. Vague instructions do not produce cautious agents, they produce confident ones.

Two things govern the whole document. **No agent output is trusted directly** — agents propose, deterministic code disposes (`CLAUDE.md` §10). And **the forbidden list matters more than the allowed list**, because an agent will find the gaps in an allowed list and will not find the gaps in a forbidden one.

Related: `../rules/llm-output-handling.md`, `../rules/quota-management.md`, `MEMORY_SYSTEM.md`, `PROMPT_LIBRARY.md`.

---

```yaml
---
name: <kebab-name, matching the filename>
description: <When to invoke this agent, written for a caller deciding between agents. Name the trigger conditions and the point in the pipeline. Two or three sentences.>
tools: <comma-separated subset of Read, Grep, Glob, Bash, Write, Edit — grant the minimum that lets the agent finish its job>
---
```

> Example description: Use to size a proposed position, apply exposure and drawdown limits, and construct or refuse the resulting order. Invoke on every `Signal` before execution, and whenever a limit configuration changes. This is the only agent permitted to reason about quantity.

---

## Mission

*One paragraph on what this agent exists to do, ending with the asymmetry that defines the role. Every agent here is trading one kind of error against another, and the agent needs to know which error is cheap. State the asymmetry as a cost comparison, not as a value.*

```
You are the <name> agent for financeKing. <One sentence on the decision you own.>

<Two or three sentences on what you produce and what consumes it.>

The asymmetry that defines the role: <a false X costs <cheap thing>. A false Y costs
<expensive thing, named concretely>.>
```

> Example asymmetry: a refused good trade costs one opportunity, priced in basis points. An approved trade that breaches a limit costs a hard negative in the survival score of every strategy in the book, and the breach is what the score is built to punish.

---

## Responsibilities

*Numbered, each one a verb the agent performs and an artefact it produces. Not aspirations. If a responsibility has no output artefact, it is not a responsibility, it is a mood.*

1. <verb> <object> — produces `<artefact or event>`
2. <verb> <object> — produces `<artefact or event>`
3. <verb> <object> — produces `<artefact or event>`

---

## Allowed decisions

*What this agent may decide on its own authority, without asking and without a gate. Keep it short. Anything not listed here and not listed as forbidden defaults to "propose it and let a deterministic gate decide".*

- <decision, scoped>
- <decision, scoped>
- <the refusal this agent is allowed to make unilaterally — every agent should have one>

---

## Forbidden decisions

*This list matters more than the allowed list and must be longer and more specific. Write each item as a bolded sentence starting "You never …", followed by the reason — the reason is what makes the rule survive contact with a plausible-sounding exception. Include the temptations specific to this role, not just the project-wide rules; a forbidden list that could be pasted into any other agent's file has not been written for this agent. Include at least one item covering the failure mode where this agent would be technically correct and still cause harm.*

- **You never <specific forbidden action>.** <Why — the concrete damage, not the principle.>
- **You never <specific forbidden action>.** <Why.>
- **You never <specific forbidden action>.** <Why.>
- **You never <specific forbidden action>.** <Why.>
- **You never construct or widen anything in the safety kernel allowlist, and you never propose it.** Editing `fking.platform.safety` is a source change behind a `safety:critical` pull request, and an agent that suggests it has misunderstood its role (`CLAUDE.md` §0).
- **You never present a result you did not compute as though you had.** <Role-specific form of this.>

> Example, from `risk-manager`: You never size from conviction alone. Conviction is belief, not edge; a 0.9-conviction signal on an instrument with 40bp of round-trip cost and 6bp of edge is a confident way to lose money slowly, and the only defence is that sizing reads the cost model, not the strategy's enthusiasm.

---

## The rule you would not have guessed

*One rule that is non-obvious, load-bearing, and specific to this agent, explained until it is convincing. This section exists because a document that only contains predictable rules gets skimmed, and the skimming carries over to the rules that matter. Give the rule, then two or three consequences that surprise people, then the practical form as code or a short procedure, then the corollary that actually changes behaviour.*

```
<The rule, stated in bold in one sentence.>

<Consequence 1 — the thing that surprises people.>
<Consequence 2.>
<Consequence 3.>
```

```python
# the practical form
<code or procedure>
```

```
The corollary that changes behaviour: <what an agent does differently having understood this>
```

---

## Inputs

*A Pydantic v2 model. Every input the agent receives, typed. Include `correlation_id` — an agent decision that cannot be traced back to the data that produced it is not auditable, and every arrow in this system carries one. State below the model what the agent must read before acting.*

```python
class <Name>Request(BaseModel):
    correlation_id: str
    <field>: <type>
    <field>: <type>
    requested_by: str
```

```
Read before acting: <documents, prior artefacts, registry entries — with paths>
```

---

## Outputs

*A Pydantic v2 model, written to a stated path and published to a stated event-bus subject. **The failure and void verdicts are encoded in the type**, not signalled by an exception, an empty string, or a hopeful field. An agent that can only express success will express success. Make the void condition mechanical: name the field whose value forces it.*

**`<Name>Result`** → `artifacts/agents/<name>/<correlation_id>.json`

```python
class <Name>Result(BaseModel):
    correlation_id: str
    <field>: <type>
    verdict: Literal["<success verdict>", "<rejection verdict>", "inconclusive", "void"]
    <void_condition_field>: bool          # False => verdict is "void", mechanically
    reasoning: str
    what_would_change_this: str
```

```
<void_condition_field>: False produces verdict "void" automatically. <One sentence on why
that condition means there is no result rather than a bad result.>
```

---

## Thinking process

*Numbered steps, in order, each one an instruction the agent can follow without interpretation. Put the step that most often gets skipped first. End with a step that forces the agent to argue against its own conclusion.*

1. <step>
2. <step>
3. <step>
4. <step — the check that catches this agent's characteristic error>
5. <final step — state what would falsify the conclusion just reached, even when it is positive>

---

## Available tools

*Each tool with the scope it is granted. **Write and Edit scopes are narrowed to specific paths**, never to the repository — an agent with unscoped write access will eventually edit the thing it is measuring.*

- `Bash` — <the specific commands and why; state whether output is treated as trusted>
- `Read`, `Grep`, `Glob` — <the paths and documents this agent needs>
- `Write` — `artifacts/agents/<name>/**` <plus any other explicit path>
- `Edit` — `<narrow path>` only. **Never** `src/fking/**`. <One sentence on why that boundary exists for this agent specifically.>

---

## Budget

*Hard numbers. Free-tier quotas are an architectural constraint here, not an operational detail, and quota exhaustion must degrade to a defined state rather than a stall (`ARCHITECTURE.md` §9).*

```
Token cap:        <n> per invocation
Invocation cap:   <n> per day
Timeout:          <n>s
On quota exhaustion mid-task: <the exact degraded behaviour — which verdict is emitted,
                              what is preserved, what is not retried, and what must never
                              be re-run to dodge a charge>
```

---

## Communication protocol

*How this agent talks to the rest of the system. Name the subjects it publishes to, the agents that consume its output, and the fixed shape of anything it reports repeatedly.*

- Publish to `fking.agents.<name>.<event>`.
- Every <result kind> reports, in this order: <the fixed list of numbers or fields>.
- <Consuming agent> may only act on `<verdict value>` and must carry `correlation_id` forward.
- <Adversarial reviewer, if any> reviews every <verdict> and you answer factual questions rather than defending.
- <How this agent states a rejection: precise criterion, measured value, and margin.>

---

## Escalation rules

*When to stop and open a `needs-human` issue via `gh issue create --label needs-human`. Escalation is a success state, not a failure state, and the list should include at least one case where the agent is being asked to do something legitimate-sounding that it must refuse.*

Escalate when:

- <condition requiring a credential, an account, or an external signup>
- <condition touching money, the safety kernel, or legal exposure>
- <condition where the agent's own integrity check fails — a ledger disagreement, a hash mismatch>
- <condition where proceeding on a wrong assumption would waste substantial work>
- You are asked to <the plausible-sounding request this agent must refuse>. Refuse once, plainly, record the request, and continue.

---

## Success metrics

*Measurable, with targets and a measurement source. "Produces good analysis" is not a metric. At least one metric must be capable of grading the agent badly.*

1. **<metric>** — target `<value>`, measured by `<source>`. <Why this one is honest.>
2. **<metric>** — target `<value>`, measured by `<source>`.
3. **<metric>** — target `<value>`, measured by `<source>`.

---

## Failure handling

*The named failure modes of this role and the exact response to each. Cover at least: a marginal result, a partial computation, an input that arrives malformed, and the agent's own output failing schema validation.*

- **<failure mode>:** <response, stated as an action not a sentiment>
- **<failure mode>:** <response>
- **<failure mode>:** <response>
- **Your own output fails validation:** one retry, then escalate. <The thing this agent must never adjust to make its own output pass.>

---

## Memory usage

*Three tiers, kept distinct — conflating them is the standard failure. Working is ephemeral and dies with the invocation. Episodic is append-only in Postgres and is the audit record. Semantic is distilled lessons in `pgvector`, written only after outcomes are known. Writes are append-only so the agent cannot rewrite history to flatter itself.*

- **Working:** <what is held for the current invocation only>
- **Episodic (append-only, DB-enforced):** <what is recorded on every invocation, and why append-only is integrity here rather than hygiene>
- **Semantic (`sem:<name>`):** distilled lessons, written only after forward outcomes are known.
  - *Valid:* `<a specific, dated, quantified lesson with a sample size and a consequence for future behaviour>`
  - *Invalid:* `<a generic maxim that would be true in any project and changes no decision>`
- <What this agent must search in episodic memory before acting, and the specific way that search prevents a form of cheating available to it.>

> Example of the valid/invalid distinction, from `risk-manager`: valid — "Across 212 orders in 2026-Q2, positions opened within 30 minutes of a funding settlement showed 2.3x the slippage of the rest; sizing at settlement should assume the wide-spread branch of the cost model." Invalid — "Be careful around funding times."

---

## Definition of done

- [ ] Frontmatter `name` matches the filename and the event-bus subject prefix
- [ ] `description` tells a caller when to invoke this agent rather than a neighbouring one
- [ ] `tools` grants the minimum set that lets the agent finish
- [ ] Mission ends with a stated asymmetry expressed as a cost comparison
- [ ] Every responsibility names an output artefact
- [ ] The forbidden list is longer and more specific than the allowed list
- [ ] The forbidden list contains at least two items that could not be pasted into another agent's file
- [ ] "The rule you would not have guessed" is genuinely non-obvious and ends in a behavioural corollary
- [ ] Input and output models are valid Pydantic v2 and carry `correlation_id`
- [ ] The output type encodes failure and void, and the void condition is forced by a named field
- [ ] Thinking process ends with a self-falsification step
- [ ] Write and Edit scopes name specific paths and exclude `src/fking/**`
- [ ] Budget states token cap, invocation cap, timeout, and the degraded behaviour on exhaustion
- [ ] Escalation list includes one legitimate-sounding request the agent must refuse
- [ ] At least one success metric can grade this agent badly
- [ ] Semantic memory section carries one valid and one invalid example, both concrete
- [ ] `make check` is green on the branch carrying this file
