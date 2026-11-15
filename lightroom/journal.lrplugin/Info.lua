-- Журнал съёмок: забирает из журнала (localhost) виды и координаты и пишет их в каталог
-- через API Lightroom. В файлы снимков плагин сам ничего не пишет.
return {
	LrSdkVersion = 6.0,
	LrSdkMinimumVersion = 6.0,
	LrToolkitIdentifier = "io.github.perevezentsev.shootingjournal",
	LrPluginName = "Журнал съёмок",
	LrPluginInfoUrl = "https://github.com/Perevezentsev/shooting-journal",
	LrPluginInfoProvider = "PluginInfo.lua",
	LrLibraryMenuItems = {
		{ title = "Журнал: забрать виды и координаты", file = "Pull.lua" },
	},
	VERSION = { major = 0, minor = 9, revision = 0 },
}
