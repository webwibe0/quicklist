import parser from '@typescript-eslint/parser';

// TypeScript checks names/types; ESLint checks runtime control-flow mistakes.
export default [
  {ignores: ['node_modules/**', '.next/**', 'next-env.d.ts']},
  {
    files: ['**/*.{ts,tsx,js,mjs,cjs}'],
    languageOptions: {parser, parserOptions: {ecmaVersion: 'latest', sourceType: 'module'}},
    rules: {
      'no-constant-condition': 'error',
      'no-dupe-args': 'error',
      'no-duplicate-case': 'error',
      'no-unreachable': 'error',
      'no-unsafe-finally': 'error',
      'valid-typeof': 'error',
    },
  },
];
