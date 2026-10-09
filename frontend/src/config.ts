
/**
 * The API origin.
 *
 * NO process.env FALLBACK. Webpack only substitutes process.env.X when a DefinePlugin says so,
 * and this build did not - so the expression threw ReferenceError: process is not defined in the
 * browser. It was invisible while the first operand was truthy, because || short-circuits; the
 * moment __AUTH_URL__ became an empty string the fallback was evaluated and the bundle died at
 * module load, before React mounted. A blank page, and the console said only "process is not
 * defined" - nothing about the URL.
 *
 * Empty is a legitimate value meaning "same origin", so it is checked explicitly rather than relying
 * on truthiness.
 */
export const AUTH_URL: string =
  typeof (window as any).__AUTH_URL__ === 'string'
    ? (window as any).__AUTH_URL__
    : '';

export const API_CONFIG = {
  baseURL: AUTH_URL,
  timeout: 10000,
};
