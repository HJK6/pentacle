import type { PentacleQuestion, PentacleQuestionItem } from '../src/index';

const nestedQuestion: PentacleQuestionItem = {
  index: 0,
  prompt: 'Pick hosts',
  options: [{ index: 1, label: 'host_c' }],
  multiSelect: true,
};

const question: PentacleQuestion = {
  prompt: 'Pick hosts',
  options: [{ index: 1, label: 'host_c' }],
  questions: [nestedQuestion],
  multiSelect: true,
};

void question;
