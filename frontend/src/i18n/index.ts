import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "./locales/en.json";
import tr from "./locales/tr.json";

export type Language = "tr" | "en";

i18n.use(initReactI18next).init({
  resources: { tr: { translation: tr }, en: { translation: en } },
  lng: "tr",
  fallbackLng: "en",
  interpolation: { escapeValue: false },
  returnNull: false,
});

export function applyLanguage(language: Language): void {
  if (i18n.language !== language) void i18n.changeLanguage(language);
  document.documentElement.lang = language;
}

export default i18n;
