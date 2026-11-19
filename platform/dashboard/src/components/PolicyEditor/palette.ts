/**
 * The dark-mode syntax inks the policy editor uses (see theme.ts), named so
 * read-only code (the quickstart's code blocks) colors tokens the same way.
 */
export const DARK_INK = {
  keyword: "#bd93f9", // keywords, operators, YAML keys
  string: "#50fa7b",
  text: "#f8f8f2",
  number: "#ffb86c",
  type: "#8be9fd", // types, urls; calls in read-only code
  comment: "#6272a4",
  heading: "#ff79c6",
} as const;
