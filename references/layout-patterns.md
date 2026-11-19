# Layout patterns

Use these as starting points, not fixed templates. Select based on the page job, current dialogue and use context; adapt the geometry to the content.

## Derive density from the requested use

Record a deck-level `density_policy` in `design_synthesis`, with page-specific exceptions in `layout_intent` where useful. Describe which content must stay on the slide, the intended reading situation, text/image balance and what can move to notes. Use free-form decisions rather than a universal word count, font size, whitespace ratio or image quota.

- For spoken projection, emphasize the main claim and evidence with quick scanning; notes can hold secondary explanation when the user allows it.
- For a compact technical defense, keep the requested assumptions, method details, comparisons and evidence together. Use aligned columns, shared headings, multi-panel figures or compact tables while retaining hierarchy.
- For independent reading, preserve the context and explanation needed without a speaker; use captions and structured paragraphs where appropriate.

These are possible use contexts, not mandatory presets. A single deck can mix a spacious opener with dense method and evidence pages. “One takeaway” permits several linked arguments, formulas or panels on that page; it does not require one sparse sentence.

When asked for tighter pages or more content, first reclaim oversized titles, decorative images, margins and repeated headers; group related evidence and add the source-backed details needed for the argument. Increase useful information rather than filler. Do not preserve a large hero image by shrinking essential text, silently remove requested detail, move it all to notes, or increase page count to avoid compact composition. If page count, content and readable sizing genuinely conflict, clarify the tradeoff.

Review representative content-heavy pages at the intended reading size. Evaluate whether the requested information is present and easy to find, not whether the page maximizes empty space. If density changes during the conversation, reflow affected pages and their image bounds; do not only change a global font-size constant.

## Interpret visual feedback before reducing content

“Too much text” can describe a page that feels like paragraphs because imagery and hierarchy do too little work. Check the current dialogue and rendered page before treating it as a request to delete information. If the user clarifies “too few images; organize the form,” retain that correction in the existing density policy and use the last content-complete version as the revision baseline. Do not retain a rejected shortened version merely because it is newer.

Choose the repair from the observed problem: shorten repetition when brevity is requested; reorganize relationships when scanning is difficult; add useful views when explanation lacks visual support; replace an unattractive subject when its appearance is the issue. For visual reorganization, map substantive claims, evidence, conditions and limitations to their new visible locations. Use annotated scenes, paired states, detail enlargements, diagrams or image-and-caption groups as appropriate. Necessary wording can remain beside the relevant object; converting prose to a diagram must preserve its meaning. Deletion, smaller fonts and moving explanation into notes are not default substitutes for composition.

Compare the revised actual render with the prior page at the same scale: can the audience see more of the process or context while still finding the required information? Record that observation in the existing visual review. Text reduction and image count alone are not success measures. Ask only if the dialogue leaves a consequential tradeoff unresolved.

## Composition examples

| Pattern | Best for | Composition cue |
|---|---|---|
| Hero claim | Opening, key result, conclusion | One large claim or number with one visual anchor |
| Figure + explanation | Method, qualitative result | Large figure on one side, concise interpretation on the other |
| Pipeline | Workflow, model architecture, experiment protocol | A directional sequence with explicit inputs and outputs |
| Layered architecture | Systems and networks | Stacked or nested layers with a clear data path |
| Comparison | Baselines, before/after, ablation | Shared axes and aligned labels; emphasize the decisive difference |
| Evidence board | Several metrics or qualitative examples | One dominant evidence block plus short annotations |
| Timeline | Training stages, continual learning, milestones | Progression along one axis with sparse checkpoints |
| Divider / transition | Section changes | A strong motif, short section title, and optional one-line promise |

For each slide, record:

```text
Page job: what the reader must understand here
Takeaway: one sentence the presenter can say
Units: text, figure, table, formula, diagram, citation, note
Relationships: order, contrast, parent/child, membership, or none
Reading order: 1 → 2 → 3 ...
Composition: the chosen pattern and its adaptation
```

Neither a dense grid nor a spacious hero split is the default. Choose the composition that fits the requested density and page job; preserve comparable geometry for related comparison pages.
