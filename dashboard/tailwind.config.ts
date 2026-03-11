import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        sidebar: {
          DEFAULT: "#1e1b4b",
          hover: "#2d2a6e",
          active: "#3730a3",
        },
      },
    },
  },
  plugins: [],
};

export default config;
