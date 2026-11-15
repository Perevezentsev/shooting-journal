-- Общие функции: связь с журналом, разбор ответа, ключевые слова, числа.
local LrHttp = import "LrHttp"
local LrPrefs = import "LrPrefs"

local Journal = {}

Journal.DEFAULT_URL = "http://localhost:8765"
Journal.ROOT_KEYWORD = "Виды"
Journal.GPS_EPS = 0.00001     -- ~1 м: координаты «те же самые»

function Journal.baseUrl()
	local url = LrPrefs.prefsForPlugin().url
	if not url or url == "" then url = Journal.DEFAULT_URL end
	return (url:gsub("/+$", ""))
end

local function urlencode(s)
	return (s:gsub("[^%w%-%._~]", function(c) return string.format("%%%02X", string.byte(c)) end))
end

-- Имя каталога: журнал сверяет, что в Lightroom открыт тот же каталог, который он читает.
function Journal.query(catalog)
	local name = catalog:getPath():match("([^/\\]+)$") or ""
	return "?catalog=" .. urlencode(name)
end

local function check(body, headers, what)
	if not body then
		error("Журнал не отвечает (" .. what .. "). Запусти его: ./run.sh, адрес — " .. Journal.baseUrl(), 0)
	end
	local status = headers and headers.status or 200
	if status ~= 200 then
		local detail = body:match('"detail"%s*:%s*"(.-)"') or body
		error("Журнал ответил " .. tostring(status) .. ": " .. detail, 0)
	end
	return body
end

function Journal.get(path, catalog)
	local body, headers = LrHttp.get(Journal.baseUrl() .. path .. Journal.query(catalog), nil, 30)
	return check(body, headers, "запрос списка")
end

function Journal.post(path, catalog, text)
	local body, headers = LrHttp.post(Journal.baseUrl() .. path .. Journal.query(catalog), text,
		{ { field = "Content-Type", value = "text/plain; charset=utf-8" } }, "POST", 60)
	return check(body, headers, "отметка записанного")
end

-- Строка с табуляциями → поля, пустые тоже сохраняются.
function Journal.split(line)
	local out = {}
	for field in (line .. "\t"):gmatch("([^\t]*)\t") do out[#out + 1] = field end
	return out
end

function Journal.lines(text)
	local out = {}
	for line in (text .. "\n"):gmatch("([^\n]*)\n") do
		line = line:gsub("\r$", "")
		if line ~= "" then out[#out + 1] = Journal.split(line) end
	end
	return out
end

-- Десятичное число из текста «60.234512» без tonumber: в русской локали Windows
-- tonumber может ждать запятую вместо точки.
function Journal.decimal(s)
	if not s or s == "" then return nil end
	local sign, int, frac = s:match("^%s*([-+]?)(%d*)%.?(%d*)%s*$")
	if not sign or (int == "" and frac == "") then return nil end
	local v = 0
	for d in int:gmatch("%d") do v = v * 10 + (d:byte() - 48) end
	local scale = 1
	for d in frac:gmatch("%d") do
		scale = scale / 10
		v = v + (d:byte() - 48) * scale
	end
	return sign == "-" and -v or v
end

function Journal.sameGps(gps, lat, lon)
	return gps and lat and lon and gps.latitude and gps.longitude
		and math.abs(gps.latitude - lat) < Journal.GPS_EPS
		and math.abs(gps.longitude - lon) < Journal.GPS_EPS
end

-- Ключевое слово по пути {"Виды", "Птицы", "Скопа"} — без создания; nil, если нет.
function Journal.findKeyword(catalog, path)
	local list = catalog:getKeywords()
	local kw
	for _, name in ipairs(path) do
		kw = nil
		for _, k in ipairs(list or {}) do
			if k:getName() == name then kw = k break end
		end
		if not kw then return nil end
		list = kw:getChildren()
	end
	return kw
end

-- Ключевое слово Виды › группа › вид, создаёт недостающее. Только внутри withWriteAccessDo.
-- «Виды» при экспорте не пишется, группа и вид — пишутся; латынь — синонимом.
function Journal.speciesKeyword(catalog, cache, group, name, latin)
	local key = group .. "\t" .. name
	if cache[key] then return cache[key] end
	if not cache["\troot"] then
		cache["\troot"] = catalog:createKeyword(Journal.ROOT_KEYWORD, {}, false, nil, true)
	end
	local gkey = "\tgroup\t" .. group
	if not cache[gkey] then
		cache[gkey] = catalog:createKeyword(group, {}, true, cache["\troot"], true)
	end
	local kw = catalog:createKeyword(name, (latin ~= "" and { latin } or {}), true, cache[gkey], true)
	if not kw then error("Не удалось создать ключевое слово «" .. name .. "»", 0) end
	cache[key] = kw
	return kw
end

return Journal
