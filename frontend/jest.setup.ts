/**
 * Jest global setup — runs once after the test framework is installed.
 *
 * Imports @testing-library/jest-dom to extend Jest's expect with DOM matchers:
 *   toBeInTheDocument, toHaveValue, toBeDisabled, toHaveTextContent, etc.
 */
import "@testing-library/jest-dom";

// jsdom does not implement scrollIntoView — stub it so auto-scroll in
// page.tsx (bottomRef.current?.scrollIntoView) doesn't throw.
window.HTMLElement.prototype.scrollIntoView = jest.fn();

// jsdom exposes crypto but not randomUUID — polyfill it so jest.spyOn works
// and the component's crypto.randomUUID() calls resolve deterministically.
if (typeof crypto.randomUUID !== "function") {
  let _counter = 0;
  Object.defineProperty(crypto, "randomUUID", {
    value: () => {
      _counter++;
      return `00000000-0000-4000-8000-${_counter.toString().padStart(12, "0")}` as `${string}-${string}-${string}-${string}-${string}`;
    },
    configurable: true,
    writable: true,
  });
}
