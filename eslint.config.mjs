export default [
  {
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        window: "readonly", document: "readonly", console: "readonly", fetch: "readonly",
        localStorage: "readonly", sessionStorage: "readonly", setTimeout: "readonly",
        clearTimeout: "readonly", setInterval: "readonly", clearInterval: "readonly",
        requestAnimationFrame: "readonly", alert: "readonly", confirm: "readonly",
        prompt: "readonly", Notification: "readonly", Audio: "readonly", Image: "readonly",
        FileReader: "readonly", Blob: "readonly", URL: "readonly", EventSource: "readonly",
        L: "readonly", io: "readonly", Chart: "readonly", QRCode: "readonly", maplibregl: "readonly",
        LEAFLET: "readonly", BASE_PATH: "writable", navigator: "readonly", location: "readonly",
        history: "readonly", performance: "readonly", crypto: "readonly", atob: "readonly",
        btoa: "readonly", TextDecoder: "readonly", TextEncoder: "readonly", AbortController: "readonly",
        structuredClone: "readonly", getComputedStyle: "readonly", matchMedia: "readonly",
        ResizeObserver: "readonly", Event: "readonly", indexedDB: "readonly",
        cancelAnimationFrame: "readonly", requestIdleCallback: "readonly", IDBKeyRange: "readonly",
        XMLHttpRequest: "readonly", WebSocket: "readonly", Worker: "readonly", Intl: "readonly"
      }
    },
    rules: { "no-undef": "error" }
  }
];
