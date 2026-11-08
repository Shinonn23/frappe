const fs = require("node:fs/promises");
const path = require("node:path");
const postcss = require("postcss");
const tailwind = require("tailwindcss");
const prefix = require("postcss-prefix-selector");
const sfc = require("@vue/compiler-sfc");

const scope = ":is(.frappe-permission-ui, #frappeui-popper-root, #headlessui-portal-root)";

async function compile(source, filename) {
	const result = await postcss([
		tailwind(require("../permission-ui.tailwind")),
		prefix({
			prefix: scope,
			transform(prefix, selector, prefixed) {
				if (
					selector === scope ||
					/^#(frappeui-popper-root|headlessui-portal-root)( > \*)?$/.test(selector)
				) {
					return selector;
				}
				if (/^(:root|html|body|:host)(?=$|[\s,:.#[])/.test(selector)) {
					return selector.replace(/^(:root|html|body|:host)/, prefix);
				}
				if (/^\[data-theme\s*=\s*['"]?dark['"]?\]$/.test(selector)) {
					return `${selector} ${prefix}`;
				}
				return prefixed;
			},
		}),
		require("autoprefixer"),
	]).process(source, { from: filename, map: false });
	return result.css;
}

const plugin = {
	name: "permission-ui-style",
	setup(build) {
		// Desk's older Vue plugin cannot compile script-only and typed library templates.
		build.onResolve({ filter: /\.vue$/ }, (args) => {
			const file = args.path.startsWith("frappe-ui/")
				? require.resolve(args.path, { paths: [args.resolveDir] })
				: path.resolve(args.resolveDir, args.path);
			if (file.includes(`${path.sep}node_modules${path.sep}frappe-ui${path.sep}`)) {
				return { path: file };
			}
		});
		build.onLoad({ filter: /node_modules[/\\]frappe-ui[/\\].*\.vue$/ }, async (args) => {
			const source = await fs.readFile(args.path, "utf8");
			const { descriptor, errors } = sfc.parse(source, { filename: args.path });
			if (errors.length) throw errors[0];
			const id = require("node:crypto")
				.createHash("sha256")
				.update(args.path)
				.digest("hex")
				.slice(0, 8);
			const script =
				descriptor.script || descriptor.scriptSetup
					? sfc.compileScript(descriptor, {
							id,
							genDefaultAs: "__permission_component",
							inlineTemplate: true,
							templateOptions: {
								compilerOptions: { expressionPlugins: ["typescript"] },
							},
					  })
					: { content: "const __permission_component = {};" };
			let contents = script.content;
			if (descriptor.template && !descriptor.scriptSetup) {
				const template = sfc.compileTemplate({
					id,
					filename: args.path,
					source: descriptor.template.content,
					compilerOptions: { expressionPlugins: ["typescript"] },
				});
				if (template.errors.length) throw template.errors[0];
				contents +=
					"\n" + template.code.replace("export function render", "function render");
				contents += "\n__permission_component.render = render;";
			}
			contents += "\nexport default __permission_component;";
			return {
				contents,
				loader: "ts",
				resolveDir: path.dirname(args.path),
				watchFiles: [args.path],
			};
		});
		build.onLoad({ filter: /[/\\]permission_ui[/\\]permission-ui\.css$/ }, async (args) => ({
			contents: `frappe.dom.set_style(${JSON.stringify(
				await compile(await fs.readFile(args.path, "utf8"), args.path)
			)});`,
			loader: "js",
			watchFiles: [
				args.path,
				path.resolve(__dirname, "../permission-ui.tailwind.js"),
				...require("fast-glob").sync(require("../permission-ui.tailwind").content),
			],
			resolveDir: path.dirname(args.path),
		}));
	},
};
module.exports = { plugin, compile, scope };
