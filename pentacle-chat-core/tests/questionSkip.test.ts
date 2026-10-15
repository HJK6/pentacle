import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import * as format from '../src/services/questionAnswerFormat';
test('shared reserved skip answer is exported',()=>assert.equal((format as unknown as Record<string,unknown>).QUESTION_SKIP_TEXT,'Skipped by operator'));
