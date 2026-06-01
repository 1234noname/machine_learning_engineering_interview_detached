import "@testing-library/jest-dom";
import { vi } from "vitest";

// jsdom does not implement the object-URL APIs that ChatInput uses for image
// thumbnail previews. Polyfill them so component tests can stage/clear uploads.
if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = vi.fn(() => "blob:mock-object-url");
}
if (typeof URL.revokeObjectURL !== "function") {
  URL.revokeObjectURL = vi.fn();
}
