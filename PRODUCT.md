# Product context

register: product

## What this is

One source in - an advisory, a report, an article, an image, a video - and one
or more finished communication artefacts out: a LinkedIn post, an executive
summary, a video package, a deck. Every factual claim in the output cites the
chunk of the source it came from, and six independent checkers judge each
artefact before an operator ever sees it.

## Users

A communications operator inside a security or research team. They have a
document and a deadline. They are not a prompt engineer and should never need
to be one. They know their audience better than the system does, and they need
to be able to say so in their own words.

They are usually watching seven artefacts race through generation and QA at
once, and the question in their head is always the same: which one is in
trouble?

## Tone

Plain and exact. The system reports what happened, including when that is
"three checks failed and I stopped". It never says "complete" while work
remains, and it never hides a blocked artefact - leaving the operator to
wonder where their deck went is worse than showing them a failure.

Confidence without swagger. No exclamation marks, no congratulation for
ordinary success.

## Anti-references

- Enterprise SaaS dashboards: card grids, hero metrics, gradient headers,
  everything the same size regardless of importance.
- Chat-bot UI: a conversation is the input method here, not the product.
- Anything that celebrates a blocked artefact. QA catching a bad draft is the
  system working, and the interface should read that way - not as a loss, and
  not as confetti either.

## Strategic principles

1. Honest reporting beats reassuring reporting. "4 of 7 done" always.
2. Every action shows that it was received. The operator should never wonder
   whether their click landed.
3. The failure is the interesting part. Fix notes, scores and retry counts are
   primary content, not diagnostics hidden behind a disclosure.
4. The operator can always override the machine. The interview proposes; the
   fields stay editable.
