-- Настройка в Plug-in Manager: адрес журнала.
local LrView = import "LrView"
local LrPrefs = import "LrPrefs"

local Journal = require "Journal"

return {
	sectionsForTopOfDialog = function(f, _)
		local prefs = LrPrefs.prefsForPlugin()
		if not prefs.url or prefs.url == "" then prefs.url = Journal.DEFAULT_URL end
		local bind = LrView.bind
		return {
			{
				title = "Журнал съёмок",
				bind_to_object = prefs,
				f:row {
					f:static_text { title = "Адрес журнала:", alignment = "right", width = LrView.share "label" },
					f:edit_field { value = bind "url", width_in_chars = 32 },
				},
				f:row {
					f:static_text { title = "", width = LrView.share "label" },
					f:static_text {
						title = "Журнал должен быть запущен (./run.sh). Меню: Библиотека → Plug-in Extras.",
					},
				},
			},
		}
	end,
}
