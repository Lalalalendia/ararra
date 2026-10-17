import test from 'node:test';
import assert from 'node:assert/strict';
import {selectBooks} from './library-view.mjs';

const books=[
  {book_id:'a',title:'Книга Альфа',owner_member_id:'mod'},
  {book_id:'b',title:'Бета',owner_member_id:'other'},
  {book_id:'c',title:'Без назначения',owner_member_id:null},
];

test('all books stay visible in shared workspace',()=>{
  assert.deepEqual(selectBooks(books,'','all','mod').map(b=>b.book_id),['a','b','c']);
});
test('my books filters responsibility without applying access controls',()=>{
  assert.deepEqual(selectBooks(books,'','mine','mod').map(b=>b.book_id),['a']);
  assert.deepEqual(selectBooks(books,'','mine','other').map(b=>b.book_id),['b']);
  assert.deepEqual(selectBooks(books,'','unassigned','mod').map(b=>b.book_id),['c']);
});
test('search combines with responsibility filter and does not evaluate HTML',()=>{
  assert.deepEqual(selectBooks(books,'альфа','mine','mod').map(b=>b.book_id),['a']);
  assert.deepEqual(selectBooks(books,'бета','mine','mod'),[]);
  assert.deepEqual(selectBooks(books,'БЕЗ','all','mod').map(b=>b.book_id),['c']);
});
