// READ-ONLY baseline for the "successful NORMAL Chrome" /apply request.
//
// PURPOSE: list the NAMES of the fields your normal Chrome sends when the
// application succeeds, so they can be compared with the names the
// Playwright run reports (APPLY_POST_REQUEST -> field_names).
//
// HOW TO USE (normal Chrome, the SAME Lever /apply page):
//   1. F12 -> Console. Click the gear icon -> tick "Preserve log"
//      (a native form submit navigates away and would otherwise clear it).
//   2. Paste this whole file, press Enter. (Chrome may ask you to type
//      "allow pasting" first.)
//   3. Fill the form and submit as you normally do.
//   4. Copy ONLY the line(s) beginning APPLY_FORM_FIELDS from the console.
//
// WHAT IT DOES: adds ONE passive, capture-phase 'submit' listener. It never
// calls preventDefault / stopPropagation, never modifies the form, and
// prints only field NAMES, whether a name is a file part (is_file), and the
// form's method / enctype / action PATH. It never prints values, filenames,
// cookies, or the CAPTCHA token.
//
// LIMITATION: if Lever submits with fetch()/XHR from a click handler and
// never fires a 'submit' event, nothing will print. Use the Network-tab
// fallback below.
(() => {
  // Set true ONLY if you want a True/False for whether captcha-named fields
  // are non-empty (never the value). Keep it false to match the default
  // Playwright run.
  const INCLUDE_CAPTCHA_NONEMPTY = false;

  const isFile = (v) => typeof File !== 'undefined' && v instanceof File;
  const report = (f) => {
    let actionPath = '';
    try {
      actionPath = new URL(f.action, location.href).pathname.replace(/[A-Za-z0-9_\-.=~%]{28,}/g, '<id>');
    } catch (e) {}
    const fields = [];
    for (const [name, v] of new FormData(f).entries()) {
      const row = { name: String(name).slice(0, 120), is_file: isFile(v) };
      if (INCLUDE_CAPTCHA_NONEMPTY && /captcha/i.test(name)) {
        row.nonempty = isFile(v) ? v.size > 0 : String(v).length > 0;
      }
      fields.push(row);
    }
    console.log('APPLY_FORM_FIELDS ' + JSON.stringify({
      at: new Date().toISOString(),
      method: (f.getAttribute('method') || 'get').toLowerCase(),
      enctype: f.enctype,
      action_path: actionPath,
      n_fields: fields.length,
      fields,
    }));
  };
  document.addEventListener('submit', (e) => { try { report(e.target); } catch (err) { console.log('APPLY_FORM_FIELDS error: ' + err); } }, true);
  console.log('Listening (read-only) for the form submit. Now fill and submit as normal.');
})();

// ---------------------------------------------------------------------------
// NETWORK-TAB FALLBACK (also gives the RESPONSE side of a successful submit)
// ---------------------------------------------------------------------------
// DevTools -> Network, tick "Preserve log", filter: apply
// After the successful submit, click the POST row ending in /apply and note
// ONLY these (do not copy values, cookies, or the CAPTCHA token):
//   - Headers   : Request Method, Status Code (e.g. 200 / 302), the response
//                 Content-Type, and Location if it redirected (path only)
//   - Headers   : Sec-Fetch-Site / Sec-Fetch-Mode / Sec-Fetch-Dest values
//   - Payload   : the field NAMES only (left column). Ignore the values.
//   - Response  : the visible message, if any (mask any personal details)
//   - Also note whether api.hcaptcha.com/checkcaptcha ran just before it, and
//     its status.
