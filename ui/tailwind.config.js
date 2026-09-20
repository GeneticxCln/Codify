/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        codify: {
          bg: "#0d1117",
          surface: "#161b22",
          border: "#30363d",
          accent: "#2f81f7",
          success: "#238636",
          warning: "#d29922",
          danger: "#f85149",
        },
      },
    },
  },
  plugins: [],
};
