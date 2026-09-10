export const QUESTION_ANSWER_FORMAT_EXAMPLES = {
  single: `Answering your question:

Q1 (Deploy): host_c`,
  multiSelect: `Answering your question:

Q1 (Toppings):
- Cheese
- Mushrooms`,
  multiQuestion: `Answering your questions:

Q1 (Host): host_c

Q2 (Priority): Now`,
  freeText: `Answering your question:

Q1 (Use case):
I need a machine
that can run tests.`,
  optionWithNote: `Answering your question:

Q1 (Deploy): workstation
note (Q1): Use the orchestrator.`,
  noteOnly: `Answering your question:

Q1 (Decision):
note (Q1): I need more context.`,
} as const;
