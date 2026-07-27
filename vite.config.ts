import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const clientPort = Number(process.env.VITE_CLIENT_PORT ?? 5173);

export default defineConfig({
  plugins: [react()],
  server: {
    port: clientPort
  }
});
