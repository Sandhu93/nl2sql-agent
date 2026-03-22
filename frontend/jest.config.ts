/**
 * Jest configuration for the NL2SQL frontend.
 *
 * To install the required devDependencies, run from frontend/:
 *
 *   npm install --save-dev \
 *     @testing-library/react \
 *     @testing-library/jest-dom \
 *     @testing-library/user-event \
 *     jest \
 *     jest-environment-jsdom \
 *     ts-jest \
 *     @types/jest \
 *     identity-obj-proxy
 *
 * Then add to package.json scripts:
 *   "test": "jest"
 */

import type { Config } from "jest";

const config: Config = {
  testEnvironment: "jsdom",
  // setupFilesAfterEnv runs after the test framework (Jest) is installed in the
  // environment — the right place for @testing-library/jest-dom matchers.
  setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
  transform: {
    "^.+\\.(ts|tsx)$": [
      "ts-jest",
      {
        tsconfig: {
          jsx: "react-jsx",
          // Relax strict settings for test files
          strict: false,
        },
      },
    ],
  },
  moduleNameMapper: {
    // Resolve @/ path alias to the frontend root
    "^@/(.*)$": "<rootDir>/$1",
    // Stub CSS modules
    "\\.(css|scss|sass)$": "identity-obj-proxy",
    // Stub static asset imports
    "\\.(png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot)$":
      "<rootDir>/__mocks__/fileMock.ts",
  },
  testMatch: [
    "**/__tests__/**/*.test.{ts,tsx}",
    "**/*.spec.{ts,tsx}",
  ],
  // Ignore Next.js build output and node_modules
  testPathIgnorePatterns: [
    "<rootDir>/node_modules/",
    "<rootDir>/.next/",
  ],
  // Collect coverage from the app source, not tests
  collectCoverageFrom: [
    "app/**/*.{ts,tsx}",
    "components/**/*.{ts,tsx}",
    "lib/**/*.{ts,tsx}",
    "!**/*.d.ts",
  ],
};

export default config;
