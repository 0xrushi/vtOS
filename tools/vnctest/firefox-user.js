// Firefox preferences for a 320x240 screen. @SCALE@ is filled in by the launcher.
// Shrinks the whole interface and pages (1.0 = normal size): lower fits more.
user_pref("layout.css.devPixelsPerPx", "@SCALE@");
user_pref("browser.uidensity", 1);                       // compact toolbar
user_pref("browser.tabs.inTitlebar", 0);                 // no custom title bar
user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);  // userChrome.css
// no first-run pages, prompts or background chatter
user_pref("browser.startup.homepage", "about:blank");
user_pref("browser.startup.page", 0);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.newtabpage.enabled", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("security.sandbox.warn_unprivileged_namespaces", false);  // Docker blocks them; the banner wastes space
user_pref("app.update.auto", false);
user_pref("app.update.enabled", false);
user_pref("extensions.pocket.enabled", false);
