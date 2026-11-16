import type { PentacleQuestion, PentacleQuestionItem } from '../types/pentacle';

export function questionPageCount(question: PentacleQuestion | null | undefined): number {
  if (!question) return 0;
  if (Array.isArray(question.questions) && question.questions.length > 0) {
    return question.questions.length;
  }
  return 1;
}

export function clampQuestionPageIndex(index: number, count: number): number {
  if (count <= 0) return 0;
  const normalized = Number.isFinite(index) ? Math.trunc(index) : 0;
  return Math.min(Math.max(normalized, 0), count - 1);
}

export function questionPageQuestion(
  question: PentacleQuestion,
  index: number,
): PentacleQuestion {
  const count = questionPageCount(question);
  const pageIndex = clampQuestionPageIndex(index, count);
  if (!Array.isArray(question.questions) || question.questions.length === 0) {
    return { ...question, active_index: pageIndex };
  }
  const item = question.questions[pageIndex] as PentacleQuestionItem | undefined;
  if (!item) return { ...question, questions: [] };
  return {
    ...question,
    multi: true,
    questions: [item],
    active_index: Number.isFinite(Number(item.index)) ? Number(item.index) : pageIndex,
  };
}
