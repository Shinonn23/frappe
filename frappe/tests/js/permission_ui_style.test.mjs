import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const postcss = require("postcss");
const { compile, scope } = require("../../../esbuild/permission-ui-style");
const filename = new URL(
  "../../public/js/frappe/permission_ui/permission-ui.css",
  import.meta.url
);

test("frappe-ui styles stay inside permission roots and their library portals", async () => {
  const css = await compile(
    await readFile(filename, "utf8"),
    filename.pathname
  );
  const root = postcss.parse(css);
  let scopedRules = 0;
  root.walkRules((rule) => {
    if (rule.parent.type === "atrule" && /keyframes$/.test(rule.parent.name))
      return;
    for (const selector of rule.selectors) {
      assert.ok(
        selector.includes(scope) ||
          /^#(frappeui-popper-root|headlessui-portal-root)( > \*)?$/.test(
            selector
          ),
        `unscoped library selector: ${selector}`
      );
    }
    scopedRules++;
  });
  assert.ok(scopedRules > 50, "compiled actual Tailwind and frappe-ui styles");
  assert.match(css, /--surface-gray-2:/, "library theme variables are present");
  assert.match(css, /z-index: 1100/, "dialogs sit above Desk chrome");
  assert.match(css, /z-index: 1110/, "popover menus sit above library dialogs");
  assert.match(
    css,
    /position: fixed/,
    "portals do not shrink Desk's flex body"
  );
  assert.match(
    css,
    /pointer-events: none/,
    "empty portal roots do not block Desk"
  );
  assert.doesNotMatch(css, /@font-face|@import/, "Desk owns fonts");
});

test("modern library templates compile alongside native Desk Vue components", async () => {
  const esbuild = require("esbuild");
  const { plugin } = require("../../../esbuild/permission-ui-style");
  const result = await esbuild.build({
    stdin: {
      contents: [
        'import "./frappe/public/js/frappe/file_uploader/FileUploader.vue";',
        'import "frappe-ui/src/components/Checkbox.vue";',
        'import "frappe-ui/src/components/FeatherIcon.vue";',
        'import "frappe-ui/src/components/Dialog.vue";',
      ].join("\n"),
      resolveDir: new URL("../../../", import.meta.url).pathname,
    },
    bundle: true,
    write: false,
    outdir: "/tmp/frappe-permission-ui-compile-test",
    plugins: [plugin, require("esbuild-plugin-vue3")()],
    define: {
      "process.env.NODE_ENV": '"production"',
      __VUE_OPTIONS_API__: "true",
      __VUE_PROD_DEVTOOLS__: "false",
      __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: "false",
    },
  });
  assert.ok(result.outputFiles.some((file) => file.path.endsWith(".js")));
});
