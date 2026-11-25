// Translation for the dashboard and configuration.
// Texts: i18n/<lang>.json (flat: "key": "text with {placeholder}").
// New language = new file in i18n/, it appears in the switcher automatically.
// Static texts in HTML: data-i18n (text), data-i18n-html (HTML), data-i18n-title, data-i18n-ph (placeholder).
const I18N = { lang: "de", locale: "de-DE", dict: {}, fallback: {}, langs: [] };

function t(key, p) {
  let s = I18N.dict[key] ?? I18N.fallback[key] ?? key;
  if (p) s = s.replace(/\{(\w+)\}/g, (m, k) => (p[k] ?? m));
  return s;
}

function i18nApply(root = document) {
  root.querySelectorAll("[data-i18n]").forEach(el => (el.textContent = t(el.dataset.i18n)));
  root.querySelectorAll("[data-i18n-html]").forEach(el => (el.innerHTML = t(el.dataset.i18nHtml)));
  root.querySelectorAll("[data-i18n-title]").forEach(el => (el.title = t(el.dataset.i18nTitle)));
  root.querySelectorAll("[data-i18n-ph]").forEach(el => (el.placeholder = t(el.dataset.i18nPh)));
  document.documentElement.lang = I18N.lang;
  const lb = document.getElementById("bLang");
  if (lb) { lb.textContent = "🌐 " + I18N.lang.toUpperCase(); lb.title = t("btn.lang_title"); }
}

async function i18nLoad(lang) {
  const get = async c => { try { const r = await fetch("i18n/" + c + ".json", { cache: "no-cache" }); return r.ok ? r.json() : null; } catch { return null; } };
  const d = await get(lang);
  if (!d) return false;
  I18N.lang = lang; I18N.dict = d; I18N.locale = d["_meta.locale"] || lang;
  if (!Object.keys(I18N.fallback).length) I18N.fallback = (lang === "en" ? d : await get("en")) || {};
  localStorage.setItem("cd_lang", lang);
  document.cookie = "cd_lang=" + lang + "; path=/; max-age=31536000; SameSite=Lax";   // for the login page and server messages
  return true;
}

async function i18nInit() {
  try { I18N.langs = await (await fetch("i18n/index.json", { cache: "no-cache" })).json(); } catch { I18N.langs = [{ code: "de" }, { code: "en" }]; }
  const codes = I18N.langs.map(l => l.code);
  const want = [new URLSearchParams(location.search).get("lang"), localStorage.getItem("cd_lang"),
    ...(navigator.languages || [navigator.language]).map(l => String(l || "").slice(0, 2).toLowerCase()), "en"];
  for (const c of want) if (c && codes.includes(c) && await i18nLoad(c)) break;
  i18nApply();
}

// Next language; onChange is called afterwards (e.g. re-render)
async function i18nNext(onChange) {
  const codes = I18N.langs.map(l => l.code);
  const next = codes[(codes.indexOf(I18N.lang) + 1) % codes.length];
  if (next && await i18nLoad(next)) { i18nApply(); onChange && onChange(); }
}
