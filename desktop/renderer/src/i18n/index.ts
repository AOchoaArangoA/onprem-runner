import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import en from './en.json';
import es from './es.json';

export type Language = 'es' | 'en';

export function resolveLanguage(setting: 'auto' | Language, browserLanguage?: string): Language {
  if (setting !== 'auto') return setting;
  const lang = browserLanguage ?? (typeof navigator !== 'undefined' ? navigator.language : 'en');
  return lang.toLowerCase().startsWith('es') ? 'es' : 'en';
}

void i18n.use(initReactI18next).init({
  resources: { es: { translation: es }, en: { translation: en } },
  lng: resolveLanguage('auto'),
  fallbackLng: 'en',
  interpolation: { escapeValue: false },
  returnNull: false,
});

export default i18n;
