-- resume-filter.lua
-- Transforms resume Markdown into formatted HTML for WeasyPrint PDF generation

local function stringify(inlines)
  return pandoc.utils.stringify(pandoc.Inlines(inlines))
end

-- Split inlines at a Span with the given class.
-- Returns (before_inlines, span_content) or (all_inlines, nil) if not found.
local function split_at_span(inlines, class)
  local before = pandoc.List()
  local span_content = nil
  for _, inline in ipairs(inlines) do
    if inline.t == "Span" and inline.classes:includes(class) then
      span_content = inline.content
    else
      before:insert(inline)
    end
  end
  -- Trim trailing spaces
  while #before > 0 and before[#before].t == "Space" do
    before:remove()
  end
  return before, span_content
end

-- Strip Strong/Emph wrappers, returning plain text inlines
local function strip_emphasis(inlines)
  local result = pandoc.List()
  for _, il in ipairs(inlines) do
    if il.t == "Strong" or il.t == "Emph" then
      result:extend(strip_emphasis(il.content))
    else
      result:insert(il)
    end
  end
  return result
end

function Pandoc(doc)
  local blocks = doc.blocks
  local new_blocks = pandoc.List()

  -- Walk blocks: transform H3 entries and org/location lines
  local i = 1
  while i <= #blocks do
    local block = blocks[i]

    if block.t == "Header" and block.level == 3 then
      local title_inlines, date_inlines = split_at_span(block.content, "date")

      if date_inlines then
        new_blocks:insert(pandoc.RawBlock('html',
          '<table class="entry-header"><tr>' ..
          '<td class="entry-title">' .. stringify(title_inlines) .. '</td>' ..
          '<td class="entry-date">' .. stringify(date_inlines) .. '</td>' ..
          '</tr></table>'
        ))

        -- Check if next block is a Para with .location span
        if i + 1 <= #blocks and blocks[i + 1].t == "Para" then
          local org_inlines, loc_inlines = split_at_span(blocks[i + 1].content, "location")
          if loc_inlines then
            new_blocks:insert(pandoc.RawBlock('html',
              '<table class="entry-org"><tr>' ..
              '<td class="entry-org-name">' .. stringify(strip_emphasis(org_inlines)) .. '</td>' ..
              '<td class="entry-org-location">' .. stringify(loc_inlines) .. '</td>' ..
              '</tr></table>'
            ))
            i = i + 1  -- skip the org/location Para
          end
        end
      else
        new_blocks:insert(block)
      end
    else
      new_blocks:insert(block)
    end

    i = i + 1
  end

  doc.blocks = new_blocks
  return doc
end
