-- Библиотека → Plug-in Extras → Журнал: забрать виды и координаты.
-- 1) берёт из журнала, что записать; 2) показывает предпросмотр с числами;
-- 3) пишет одной операцией (Ctrl+Z отменяет целиком); 4) сообщает журналу, что записано.
local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrFunctionContext = import "LrFunctionContext"
local LrProgressScope = import "LrProgressScope"

local Journal = require "Journal"

local function plural(n, one, few, many)
	local n10, n100 = n % 10, n % 100
	if n10 == 1 and n100 ~= 11 then return one end
	if n10 >= 2 and n10 <= 4 and (n100 < 12 or n100 > 14) then return few end
	return many
end

local function frames(n) return n .. " " .. plural(n, "кадр", "кадра", "кадров") end

LrFunctionContext.postAsyncTaskWithContext("Журнал: виды и координаты", function(context)
	LrDialogs.attachErrorDialogToFunctionContext(context)
	local catalog = LrApplication.activeCatalog()

	-- 1. Что записать
	local rows = Journal.lines(Journal.get("/api/lr/pending", catalog))
	if not rows[1] or rows[1][1] ~= "journal" then
		error("Журнал ответил непонятно — обнови плагин из папки журнала.", 0)
	end
	table.remove(rows, 1)
	if #rows == 0 then
		LrDialogs.message("Журнал: всё уже записано", "Новых видов и координат нет.", "info")
		return
	end

	local progress = LrProgressScope { title = "Журнал: сверяю кадры с каталогом", functionContext = context }
	local jobs = { S = {}, R = {}, G = {}, X = {}, H = {} }
	local missing, gpsPhotos = 0, {}
	for i, r in ipairs(rows) do
		local photo = catalog:getPhotoByLocalId(tonumber(r[2]))
		if jobs[r[1]] then
			if photo then
				table.insert(jobs[r[1]], { row = r, photo = photo })
				if r[1] == "G" or r[1] == "X" or r[1] == "H" then gpsPhotos[#gpsPhotos + 1] = photo end
			elseif r[1] == "H" then
				-- кадра в каталоге нет — и координат у него нет
			elseif r[1] == "R" or r[1] == "X" then
				-- кадра в каталоге нет — снимать нечего, журнал просто забудет запись
				table.insert(jobs[r[1]], { row = r })
			else
				missing = missing + 1
			end
		end
		if i % 500 == 0 then progress:setPortionComplete(i, #rows) end
		if progress:isCanceled() then return end
	end
	progress:done()

	-- 2. Свои координаты кадров не трогаем: пишем только тем, у кого их нет
	--    или стоят записанные журналом раньше.
	local gpsNow = #gpsPhotos > 0 and catalog:batchGetRawMetadata(gpsPhotos, { "gps" }) or {}
	local writeGps, sameGps, ownGps, clearGps, keepX = {}, {}, {}, {}, {}
	for _, j in ipairs(jobs.G) do
		local gps = (gpsNow[j.photo] or {}).gps
		j.lat, j.lon = Journal.decimal(j.row[3]), Journal.decimal(j.row[4])
		local oldLat, oldLon = Journal.decimal(j.row[5]), Journal.decimal(j.row[6])
		if Journal.sameGps(gps, j.lat, j.lon) then
			sameGps[#sameGps + 1] = j
		elseif not gps or not gps.latitude or Journal.sameGps(gps, oldLat, oldLon) then
			writeGps[#writeGps + 1] = j
		else
			ownGps[#ownGps + 1] = j
		end
	end
	for _, j in ipairs(jobs.X) do
		local gps = j.photo and (gpsNow[j.photo] or {}).gps
		if j.photo and Journal.sameGps(gps, Journal.decimal(j.row[3]), Journal.decimal(j.row[4])) then
			clearGps[#clearGps + 1] = j
		else
			keepX[#keepX + 1] = j      -- координаты уже другие (поставил сам) — не трогаем
		end
	end

	-- Рядом со скрытыми находками координат в каталоге быть не должно, кто бы их ни поставил
	-- (журнал, автопривязка по треклогу, вручную). Проверяется при каждом запуске.
	local hideGps, hideClean = {}, {}
	for _, j in ipairs(jobs.H) do
		local gps = (gpsNow[j.photo] or {}).gps
		if gps and gps.latitude then hideGps[#hideGps + 1] = j else hideClean[#hideClean + 1] = j end
	end

	local changedKw = 0
	for _, j in ipairs(jobs.S) do
		if j.row[7] ~= "" then changedKw = changedKw + 1 end
	end

	-- 3. Предпросмотр
	local info = {}
	local function add(s) info[#info + 1] = s end
	if #jobs.S > 0 then
		add("Ключевые слова видов (" .. Journal.ROOT_KEYWORD .. " › группа › вид): " .. frames(#jobs.S))
		if changedKw > 0 then add("   из них замена вида: " .. frames(changedKw)) end
	end
	if #jobs.R > 0 then add("Снять слово вида (в журнале вида больше нет): " .. frames(#jobs.R)) end
	if #writeGps > 0 then add("Координаты по треку: " .. frames(#writeGps)) end
	if #ownGps > 0 then add("Пропущу — у кадра свои координаты: " .. frames(#ownGps)) end
	if #clearGps > 0 then add("Убрать координаты, записанные раньше (рядом скрытая находка или кадр вне трека): " .. frames(#clearGps)) end
	if #hideGps > 0 then add("Уберу координаты (ближе 200 м к скрытым находкам): " .. frames(#hideGps)) end
	if missing > 0 then add("Нет в этом каталоге: " .. frames(missing)) end
	local toWrite = #jobs.S + #jobs.R + #writeGps + #clearGps + #hideGps
	if toWrite == 0 then
		if #sameGps + #ownGps + #keepX + #hideClean > 0 then
			local done = {}
			for _, j in ipairs(sameGps) do done[#done + 1] = "G\t" .. j.row[2] .. "\t" .. j.row[3] .. "\t" .. j.row[4] end
			for _, j in ipairs(ownGps) do done[#done + 1] = "O\t" .. j.row[2] end
			for _, j in ipairs(keepX) do done[#done + 1] = "X\t" .. j.row[2] end
			for _, j in ipairs(hideClean) do done[#done + 1] = "X\t" .. j.row[2] end
			Journal.post("/api/lr/done", catalog, table.concat(done, "\n"))
		end
		LrDialogs.message("Журнал: записывать нечего", #info > 0 and table.concat(info, "\n") or "Новых видов и координат нет.", "info")
		return
	end
	local result = table.concat(info, "\n")
	if LrDialogs.confirm("Журнал: записать в каталог?", result .. "\n\nДругие ключевые слова не трогаю. Всё запишется "
			.. "одной операцией — Правка → Отменить вернёт как было.", "Записать", "Отмена") ~= "ok" then
		return
	end

	-- 4. Запись одной операцией
	local done, failed = {}, 0
	local function mark(s) done[#done + 1] = s end
	-- Снять координаты кадра (и высоту, если её поставила автопривязка). false — Lightroom не дал.
	local function clearGpsOf(photo)
		local ok = pcall(function() photo:setRawMetadata("gps", nil) end)
		pcall(function() photo:setRawMetadata("gpsAltitude", nil) end)
		return ok
	end
	local status = catalog:withWriteAccessDo("Журнал: виды и координаты", function()
		local cache = {}
		local wp = LrProgressScope { title = "Журнал: записываю виды и координаты" }
		local total, n = toWrite, 0
		local function step()
			n = n + 1
			if n % 200 == 0 then wp:setPortionComplete(n, total) end
		end
		for _, j in ipairs(jobs.S) do
			local r = j.row
			local kw = Journal.speciesKeyword(catalog, cache, r[3], r[4], r[5])
			if r[7] ~= "" and (r[6] ~= r[3] or r[7] ~= r[4]) then
				local old = Journal.findKeyword(catalog, { Journal.ROOT_KEYWORD, r[6], r[7] })
				if old then j.photo:removeKeyword(old) end
			end
			j.photo:addKeyword(kw)
			mark(table.concat({ "S", r[2], r[3], r[4], r[5] }, "\t"))
			step()
		end
		for _, j in ipairs(jobs.R) do
			local r = j.row
			if j.photo then
				local old = Journal.findKeyword(catalog, { Journal.ROOT_KEYWORD, r[3], r[4] })
				if old then j.photo:removeKeyword(old) end
			end
			mark("R\t" .. r[2])
			step()
		end
		for _, j in ipairs(writeGps) do
			j.photo:setRawMetadata("gps", { latitude = j.lat, longitude = j.lon })
			mark("G\t" .. j.row[2] .. "\t" .. j.row[3] .. "\t" .. j.row[4])
			step()
		end
		for _, list in ipairs({ clearGps, hideGps }) do
			for _, j in ipairs(list) do
				if clearGpsOf(j.photo) then mark("X\t" .. j.row[2]) else failed = failed + 1 end
				step()
			end
		end
		wp:done()
	end, { timeout = 60 })
	if status ~= "executed" then
		error("Каталог занят другой операцией — ничего не записано, попробуй ещё раз.", 0)
	end

	for _, j in ipairs(sameGps) do mark("G\t" .. j.row[2] .. "\t" .. j.row[3] .. "\t" .. j.row[4]) end
	for _, j in ipairs(ownGps) do mark("O\t" .. j.row[2]) end
	for _, j in ipairs(keepX) do mark("X\t" .. j.row[2]) end
	for _, j in ipairs(hideClean) do mark("X\t" .. j.row[2]) end

	-- 5. Журнал запоминает записанное: следующий запуск возьмёт только новое
	Journal.post("/api/lr/done", catalog, table.concat(done, "\n"))
	if failed > 0 then
		result = result .. "\n\nНе удалось убрать координаты: " .. frames(failed)
			.. ". Сними их вручную: Метаданные → GPS, или напиши в журнал."
	end
	LrDialogs.message(failed > 0 and "Журнал: записано, но не всё" or "Журнал: записано", result, failed > 0 and "warning" or "info")
end)
