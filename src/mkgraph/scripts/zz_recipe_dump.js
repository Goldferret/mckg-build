// zz_recipe_dump.js
// Dumps all loaded recipes (after all KubeJS modifications) to kubejs/dumps/
// Named zz_ so it runs last alphabetically, capturing the final post-modification state.
//
// KubeJS 2101 Rhino quirks this script works around:
//   - `const`/`let` inside forEachRecipe callbacks are not properly re-scoped → use `var`
//   - `String(recipe.type)` and `recipe.type.toString()` throw → get type from recipe.json
//   - `java.*` and `Java.type()` are blocked by class filter → use JsonIO for file writing
//
// Output: kubejs/dumps/recipes_000.json, recipes_001.json, ...
//         kubejs/dumps/recipes_meta.json

var CHUNK_SIZE = 5000

ServerEvents.recipes(event => {
    var startTime = Date.now()
    console.log('[RecipeDump] Starting recipe dump...')

    var chunk      = []
    var chunkIndex = 0
    var total      = 0
    var skipped    = 0
    var byType     = {}
    var byMod      = {}

    function flushChunk() {
        if (chunk.length === 0) return
        var filename = 'kubejs/dumps/recipes_' + String(chunkIndex).padStart(3, '0') + '.json'
        JsonIO.write(filename, chunk)
        console.log('[RecipeDump] Wrote chunk ' + chunkIndex + ' (' + chunk.length + ' recipes)')
        chunkIndex++
        chunk = []
    }

    event.forEachRecipe({}, recipe => {
        try {
            // Get all fields from recipe.json — avoids touching recipe.type (throws in Rhino 2101)
            var jsonStr = recipe.json.toString()
            var rJson   = JSON.parse(jsonStr)
            var rId     = String(recipe.id)
            var rType   = rJson.type || 'unknown'
            var rMod    = rId.includes(':') ? rId.split(':')[0] : 'unknown'

            chunk.push({
                id:   rId,
                type: rType,
                mod:  rMod,
                data: rJson
            })

            total++
            byType[rType] = (byType[rType] || 0) + 1
            byMod[rMod]   = (byMod[rMod]   || 0) + 1

            if (chunk.length >= CHUNK_SIZE) flushChunk()
        } catch (e) {
            skipped++
        }
    })

    flushChunk()

    var meta = {
        timestamp:  new Date().toISOString(),
        total:      total,
        skipped:    skipped,
        chunks:     chunkIndex,
        chunk_size: CHUNK_SIZE,
        format:     'json_chunks',
        by_type:    byType,
        by_mod:     byMod
    }

    JsonIO.write('kubejs/dumps/recipes_meta.json', meta)

    var elapsed = ((Date.now() - startTime) / 1000).toFixed(2)
    console.log('[RecipeDump] Done: ' + total + ' recipes in ' + chunkIndex + ' chunks, ' + elapsed + 's (' + skipped + ' skipped)')
})
