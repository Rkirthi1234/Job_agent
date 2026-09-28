// READ-ONLY baseline for the "NORMAL MANUAL CHROME" column.
//
// Use: open the SAME Lever application page in your normal Chrome, press F12,
// open the Console tab, (Chrome may ask you to type "allow pasting" first),
// paste this whole file, press Enter. The JSON is printed AND copied to your
// clipboard. It only reads navigator/screen/window values and counts DOM
// nodes; it does not click, type, or modify anything, and it does not read
// cookies or any form field.
//
// Also note by hand (not available from a page script):
//   - chrome://version  -> "Profile Path", and whether this is a normal
//     profile, Guest, or Incognito window
//   - chrome://settings/cookies/detail?site=lever.co  -> whether you already
//     have Lever / hCaptcha cookies (count only; do not copy values)
(() => {
  const n = navigator;
  const uad = n.userAgentData;
  const ro = Intl.DateTimeFormat().resolvedOptions();
  const count = (sel) => document.querySelectorAll(sel).length;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const submitByText = Array.from(document.querySelectorAll('button')).filter(
    (b) => /submit application/i.test(b.innerText || b.textContent || '')
  );
  const submitByType = Array.from(document.querySelectorAll("button[type='submit']"));
  const describe = (b) => ({
    visible: visible(b),
    enabled: !b.disabled,
    tag_name: b.tagName.toLowerCase(),
    type_attr: b.getAttribute('type'),
    id_attr: b.id || null,
    class_attr: b.getAttribute('class'),
    text: (b.innerText || b.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120),
  });
  const out = {
    userAgent: n.userAgent,
    webdriver: 'webdriver' in n ? n.webdriver : 'absent',
    language: n.language,
    languages: Array.from(n.languages || []),
    platform: n.platform,
    hardwareConcurrency: n.hardwareConcurrency,
    pluginsLength: n.plugins ? n.plugins.length : null,
    screenWidth: screen.width,
    screenHeight: screen.height,
    screenAvailWidth: screen.availWidth,
    screenAvailHeight: screen.availHeight,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    outerWidth: window.outerWidth,
    outerHeight: window.outerHeight,
    devicePixelRatio: window.devicePixelRatio,
    timeZone: ro.timeZone, // property is `timeZone` (capital Z)
    intlLocale: ro.locale,
    uaBrands: uad ? uad.brands.map((b) => b.brand + '/' + b.version) : null,
    uaMobile: uad ? uad.mobile : null,
    cookieEnabled: n.cookieEnabled,
    maxTouchPoints: n.maxTouchPoints,
    visibilityState: document.visibilityState,
    hasFocus: document.hasFocus(),
    submit_button_matches: {
      "button (text contains 'Submit application')": submitByText.map(describe),
      "button[type='submit']": submitByType.map(describe),
    },
    captcha: {
      'iframe[src*=recaptcha/api2/bframe]': count("iframe[src*='recaptcha/api2/bframe']"),
      'iframe[title*=recaptcha challenge]': count("iframe[title*='recaptcha challenge' i]"),
      'iframe[src*=hcaptcha]': count("iframe[src*='hcaptcha']"),
      body_has_not_a_robot: /i'm not a robot/i.test(document.body.innerText),
      body_has_hcaptcha: /hcaptcha/i.test(document.body.innerText),
    },
  };
  const text = JSON.stringify(out, null, 2);
  console.log(text);
  try { copy(text); console.log('(copied to clipboard)'); } catch (e) {}
  return out;
})();
