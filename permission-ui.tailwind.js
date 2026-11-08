const path = require("node:path");

module.exports = {
	presets: [require("frappe-ui/src/tailwind/preset")],
	content: [
		path.join(__dirname, "frappe/public/js/frappe/permission_ui/*.{vue,js}"),
		path.join(__dirname, "node_modules/frappe-ui/src/components/**/*.{vue,js,ts}"),
	],
};
