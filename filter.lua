-- resume-filter.lua
-- Transforms resume Markdown into formatted HTML (for WeasyPrint PDF) and DOCX

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

-- Create a borderless two-column Pandoc Table (for DOCX output)
local function make_two_col_table(left_inlines, right_inlines)
  local simple = pandoc.SimpleTable(
    {},
    {pandoc.AlignLeft, pandoc.AlignRight},
    {0, 0},
    {{}, {}},
    {
      {{pandoc.Plain(left_inlines)}, {pandoc.Plain(right_inlines)}}
    }
  )
  return pandoc.utils.from_simple_table(simple)
end

function Pandoc(doc)
  local meta = doc.meta
  local blocks = doc.blocks
  local new_blocks = pandoc.List()
  local is_html = FORMAT:match('html')
  local is_docx = FORMAT:match('docx')

  -- For DOCX: inject header from YAML metadata
  if is_docx then
    local email = pandoc.utils.stringify(meta.email or "")
    local phone = pandoc.utils.stringify(meta.phone or "")
    local name = pandoc.utils.stringify(meta.name or "")
    local subtitle = pandoc.utils.stringify(meta.subtitle or "")

    if email ~= "" then
      new_blocks:insert(pandoc.Div(
        {pandoc.Para({
          pandoc.Link(pandoc.Str(email), "mailto:" .. email),
          pandoc.Space(), pandoc.Str("•"), pandoc.Space(),
          pandoc.Link(pandoc.Str(phone), "tel:" .. phone)
        })},
        pandoc.Attr("", {}, {{"custom-style", "headerDetails"}})
      ))
    end

    if name ~= "" then
      new_blocks:insert(pandoc.Header(1, pandoc.Inlines({pandoc.Str(name)})))
    end

    if subtitle ~= "" then
      new_blocks:insert(pandoc.Div(
        {pandoc.Para({pandoc.Str(subtitle)})},
        pandoc.Attr("", {}, {{"custom-style", "headerPosition"}})
      ))
    end
  end

  -- Walk blocks: transform H3 entries and org/location lines
  local i = 1
  while i <= #blocks do
    local block = blocks[i]

    if block.t == "Header" and block.level == 3 then
      local title_inlines, date_inlines = split_at_span(block.content, "date")

      if date_inlines then
        if is_html then
          new_blocks:insert(pandoc.RawBlock('html',
            '<div class="entry-header"><span class="entry-title">' ..
            stringify(title_inlines) ..
            '</span><span class="entry-date">' ..
            stringify(date_inlines) ..
            '</span></div>'
          ))
        elseif is_docx then
          new_blocks:insert(make_two_col_table(
            {pandoc.Strong(strip_emphasis(title_inlines))},
            {pandoc.Strong(date_inlines)}
          ))
        else
          new_blocks:insert(block)
        end

        -- Check if next block is a Para with .location span
        if i + 1 <= #blocks and blocks[i + 1].t == "Para" then
          local org_inlines, loc_inlines = split_at_span(blocks[i + 1].content, "location")
          if loc_inlines then
            if is_html then
              new_blocks:insert(pandoc.RawBlock('html',
                '<div class="entry-org"><span class="entry-org-name">' ..
                stringify(strip_emphasis(org_inlines)) ..
                '</span><span class="entry-org-location">' ..
                stringify(loc_inlines) ..
                '</span></div>'
              ))
            elseif is_docx then
              new_blocks:insert(make_two_col_table(
                {pandoc.Strong({pandoc.Emph(strip_emphasis(org_inlines))})},
                {pandoc.Strong({pandoc.Emph(loc_inlines)})}
              ))
            end
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
