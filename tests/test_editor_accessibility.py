"""Keyboard, recovery and rendering contracts for the editor."""
import re

import pytest

from test_web_i18n import browser
from test_web_i18n import WEB


@pytest.mark.parametrize("drawer", [False, True])
def test_search_retains_layout_controls_images_and_folder_name_draft(drawer):
    browser("""
$('#pages').querySelector('[data-id="synthetic.folder"]').click();
if(!DRAWER)$('#drawer-close').click();
const folder=$('#pages').querySelector('[data-id="synthetic.folder"]');
const items=[...document.querySelectorAll('.item[data-id]')];
const widgets=[...document.querySelectorAll('.widget[data-id]')];
const images=[...document.querySelectorAll('img')];
const rows=[...$('#library').children];
const before=JSON.stringify(vm.runInContext('V',context));
const first=$('#dock').firstChild;first.dispatchEvent({type:'click',shiftKey:true});
$('#folder-name').value='Draft 雪 {name}';$('#folder-name').focus();
for(const raw of ['  ALPHA ', 'alpha', '']) {
  $('#search').value=raw;$('#search').dispatchEvent({type:'input'});
  for(const [selector,expected] of [['.item[data-id]',items],['.widget[data-id]',widgets],['img',images]]) {
    const actual=document.querySelectorAll(selector);
    assert.equal(actual.length,expected.length);
    assert.ok(actual.every((node,i)=>node===expected[i]),selector+' identity');
  }
  assert.ok($('#library').children.every((node,i)=>node===rows[i]));
  assert.equal(first.getAttribute('aria-pressed'),'true');
  assert.equal($('#search').value,raw);
  assert.equal($('#folder-name').value,'Draft 雪 {name}');
  assert.equal(document.activeElement,$('#folder-name'));
  assert.equal(JSON.stringify(vm.runInContext('V',context)),before);
  assert.equal(folder.classList.contains('hit'),Boolean(raw));
  assert.equal($('#bank').firstChild.classList.contains('dim'),Boolean(raw));
  for(const node of widgets) {
    assert.equal(node.classList.contains('hit'),false);
    assert.equal(node.classList.contains('dim'),false);
  }
  const child=$('#folder-pages').querySelector('[data-id="synthetic.app"]');
  assert.equal(child.classList.contains('hit'),Boolean(raw)&&DRAWER);
}
""".replace("DRAWER", str(drawer).lower()), native=True, setup="""
fixture.pages[0][0].pages[0][0].name='Alpha child';
fixture.pages[0].push({id:'synthetic.widget',kind:'widget',name:'Alpha widget',size:'small',icon:'widget.icon',config:{opaque:['雪',17]}});
fixture.dock=[{id:'dock.alpha',name:'Alpha dock',kind:'app',icon:'dock.icon'}];
fixture.bank=[{id:'bank.other',name:'Other',kind:'app',icon:'bank.icon'}];
fixture.library=[{id:'lib.alpha',name:'Alpha library',kind:'app',icon:'lib.icon'}];
""")


@pytest.mark.parametrize("kind", ["error", "ok"])
@pytest.mark.parametrize("dark", [False, True])
def test_toast_diagnostic_and_neutral_button_contrast(dark, kind):
    css = (WEB / "app.css").read_text()

    def declarations(selector):
        matches = re.findall(r"(?m)^" + re.escape(selector) + r"\s*\{([^{}]*)\}", css)
        return dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)", ";".join(matches)))

    roots = re.findall(r":root\s*\{([^{}]*)\}", css)
    colors = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+)", roots[0]))
    if dark:
        colors.update(re.findall(r"(--[\w-]+)\s*:\s*([^;]+)", roots[1]))

    def resolve(value, inherited):
        value = value.strip()
        if value == "inherit":
            return inherited
        return colors[value[4:-1]] if value.startswith("var(") else value

    def luminance(color):
        color = color.lstrip("#")
        if len(color) == 3:
            color = "".join(c * 2 for c in color)
        rgb = [int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        rgb = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
        return sum(c * weight for c, weight in zip(rgb, (.2126, .7152, .0722)))

    def ratio(a, b):
        a, b = sorted((luminance(a), luminance(b)))
        return (b + .05) / (a + .05)

    error = declarations(".toast." + kind)
    background = resolve(error["background"], colors["--fg"])
    foreground = resolve(error["color"], colors["--bg"])
    detail = declarations(".diagnostic") | declarations(".toast." + kind + " .diagnostic")
    button = declarations("button") | declarations(".toast." + kind + " button")
    assert ratio(foreground, background) >= 4.5
    assert ratio(resolve(detail["color"], foreground), background) >= 4.5
    assert ratio(resolve(button["color"], foreground), resolve(button["background"], background)) >= 4.5


def test_unloaded_saved_draft_requires_reviewed_read():
    browser("""
const binding = {device:'synthetic', revision:'damaged'};
context.setView({loaded:false, binding, draft_error:'Fixture diagnostic'});
assert.equal($('#draft-recovery pre').textContent, 'Fixture diagnostic');
const requests = [];
const prior = context.fetch;
context.fetch = async (path, opts) => {
  if (path !== '/api/read') return prior(path,opts);
  requests.push(JSON.parse(opts.body));
  return {ok:true,json:async()=>fixture};
};
$('#read-empty').click();await tick();
assert.equal(document.activeElement, $('#modal-actions').firstChild);
$('#modal-actions').firstChild.click();await tick();
assert.deepEqual(requests, []);
$('#read').click();await tick();
context.setView({loaded:false,binding:{device:'synthetic',revision:'newer'},draft_error:'New diagnostic'});
$('#modal-actions').lastChild.click();await tick();await tick();
assert.deepEqual(requests,[{binding}]);assert.equal($('#draft-recovery').hidden,true);
""", native=True)


def test_folder_subpage_choices_disambiguate_only_matching_labels():
    browser("""
$('#pages').querySelector('[data-id="synthetic.app"]').click();
$('#sel-place').click();
const options = $('#item-target').children;
const label = value => options.find(option => option.value === value).textContent;
assert.notEqual(label('folder:first:1'),label('folder:second:1'));
assert.equal(label('folder:unique:1'),'Study · ' + i18n.t('editor.page',{number:1}));
assert.equal(new Set(options.map(option=>option.value)).size, options.length);
""", native=True, setup="""
const f = (id,name)=>({id,kind:'folder',name,pages:[[]]});
fixture.dock=[f('folder:first','Work')];
fixture.pages=[[item,f('folder:second','Work'),f('folder:unique','Study')]];
""")


def test_display_equivalent_folder_names_get_qualifiers_and_keep_raw_data():
    browser("""
$('#pages').querySelector('[data-id="synthetic.app"]').click();$('#sel-place').click();
const options=$('#item-target').children;
for(const folder of fixture.dock) {
  const value=folder.id+':1', text=options.find(x=>x.value===value).textContent;
  assert.ok(text.startsWith(folder.name+' · '));
  assert.equal(text.endsWith('['+folder.id+']'),folder.id!=='folder:nbsp');
}
""", native=True, setup="""
fixture.pages=[[item]];
fixture.dock=[['first','Work  Desk'],['second','Work Desk'],['nbsp','Work Desk']]
  .map(([id,name])=>({id:'folder:'+id,kind:'folder',name,pages:[[]]}));
""")


def test_folder_only_and_unavailable_library_actions_use_eligible_selection():
    browser("""
$('#pages').querySelector('[data-id="synthetic.folder"]').dispatchEvent({type:'click',shiftKey:true});
assert.equal($('#sel-folder').disabled, true);
assert.equal($('#sel-library').disabled, true);
assert.equal($('#sel-place').disabled, false);
$('#sel-clear').click();
const selected=$('#pages').querySelector('[data-id="synthetic.app"]');
selected.click();
assert.equal($('#sel-folder').disabled,false);
assert.equal($('#sel-library').disabled,true);
document.dispatchEvent({type:'keydown',key:'Delete',target:selected});
await tick();assert.equal(calls.filter(path=>path==='/api/op').length,0);
""", native=True, setup="fixture.pages[0][0].pages=[[]];fixture.pages[0].push(item);fixture.library=null;")


def test_open_organize_closes_when_the_layout_becomes_unavailable():
    browser("""
$('#organize-btn').click();
assert.equal($('#organize-btn').getAttribute('aria-expanded'),'true');
const command=$('#organize-menu').querySelector('[data-op="sort_alpha"]');
command.focus();context.setView({loaded:false,binding:fixture.binding});
assert.equal($('#organize-menu').hidden,true);
assert.equal($('#organize-btn').getAttribute('aria-expanded'),'false');
assert.equal($('#organize-btn').disabled,true);
assert.equal(document.activeElement,$('#read-empty'));
""", native=True)


def test_selection_and_equivalent_library_results_preserve_mounted_controls():
    browser("""
const home=$('#pages').querySelector('[data-id="synthetic.app"]');
const rows=[...$('#library').children];
home.focus();home.dispatchEvent({type:'keydown',key:' '});
assert.equal($('#pages').querySelector('[data-id="synthetic.app"]'),home);
assert.equal(home.getAttribute('aria-pressed'),'true');
assert.equal(document.activeElement,home);
for(const query of ['lib','libr']) {
  $('#search').value=query;$('#search').oninput({target:$('#search')});
  assert.deepEqual($('#library').children,rows);
}
const changed=JSON.parse(JSON.stringify(fixture));
changed.binding.revision='next';changed.moved=[rows[0].dataset.id];
context.setView(changed);
assert.deepEqual($('#library').children,rows);
assert.equal(rows[0].classList.contains('moved'),true);
rows[0].dispatchEvent({type:'click',shiftKey:true});
assert.equal(rows[0].getAttribute('aria-pressed'),'true');
""", native=True, setup="""
fixture.pages=[[item]];
fixture.library=[{id:'lib.b',kind:'app',name:'Library B'},{id:'lib.a',kind:'app',name:'Library A'}];
""")


def test_extra_undo_modifiers_and_uncommitted_folder_name_escape():
    browser("""
const app=$('#pages').querySelector('[data-id="synthetic.folder"]');
for(const extra of [{ctrlKey:true,altKey:true},{metaKey:true,altKey:true},
                    {ctrlKey:true,metaKey:true},{ctrlKey:true,isComposing:true}]) {
  document.dispatchEvent({type:'keydown',key:'z',target:app,...extra});
}
app.click();const input=$('#folder-name');input.value='Uncommitted';
document.dispatchEvent({type:'keydown',key:'Escape',target:input});
await tick();assert.equal($('#drawer').hidden,true);
assert.equal(document.activeElement.dataset.id,'synthetic.folder');
assert.equal(calls.filter(p=>p==='/api/op').length,0);
""", native=True)


def test_clear_of_a_filtered_library_selection_returns_focus_to_search():
    browser("""
const library=$('#library');library.children[0].click();
const search=$('#search');search.value='  Absent  ';search.oninput({target:search});
$('#sel-clear').focus();$('#sel-clear').click();
assert.equal(document.activeElement,search);
assert.equal(search.value,'  Absent  ');
assert.equal($('#selection-bar').hidden,true);
assert.equal(calls.filter(x=>x==='/api/op').length,0);
""", native=True, setup="fixture.library=[{id:'lib',kind:'app',name:'Notes'}];")


def test_open_drawer_reserves_the_library_and_keeps_the_exposed_board_focusable():
    browser("""
const drawer=$('#drawer'),board=$('#board'),library=$('#library-panel');
drawer.rect={left:700,right:1100,width:400};board.rect={left:0,right:700,width:700};
const folder=board.querySelector('[data-id="synthetic.folder"]');folder.click();
assert.equal(library.inert,true);assert.equal(board.inert,false);
board.querySelector('[data-id="synthetic.folder"]').focus();drawer.rect={left:14,right:414,width:400};
document.dispatchEvent({type:'resize'});
assert.equal(board.inert,true);assert.equal(document.activeElement,$('#folder-name'));
$('#drawer-close').click();
assert.equal(library.inert,false);assert.equal(board.inert,false);
""", native=True)


def test_drawer_board_focus_requires_room_for_a_laid_out_grid_cell():
    browser("""
const drawer=$('#drawer'),board=$('#board'),pages=$('#pages');
drawer.rect={left:200,right:600,width:400};board.rect={left:0,right:200,width:200};
pages.rect={left:20,right:180,width:160};
board.querySelector('[data-id="synthetic.folder"]').click();
for(const tile of board.querySelectorAll('.tile'))tile.rect={left:0,right:58,width:58};
document.dispatchEvent({type:'resize'});
assert.equal(board.inert,false);
board.querySelector('[data-id="synthetic.folder"]').focus();
drawer.rect={left:80,right:480,width:400};board.rect={left:0,right:80,width:80};
pages.rect={left:20,right:60,width:40};
document.dispatchEvent({type:'resize'});
assert.equal(board.inert,true);assert.equal(document.activeElement,$('#folder-name'));
drawer.rect={left:200,right:600,width:400};board.rect={left:0,right:200,width:200};
pages.rect={left:20,right:180,width:160};
document.dispatchEvent({type:'resize'});
assert.equal(board.inert,false);assert.equal($('#library-panel').inert,true);
""", native=True)


def test_read_retries_only_failed_images_on_retained_library_rows():
    browser("""
const list=$('#library');
const [missing,healthy,noIcon]=list.children;
const failedTile=missing.firstChild, goodImage=healthy.firstChild.firstChild;
failedTile.firstChild.remove();failedTile.textContent='A';
context.setView(JSON.parse(JSON.stringify(fixture)));
assert.equal(missing.firstChild,failedTile);
$('#read').click();await tick();await tick();
assert.equal(list.children[0],missing);assert.notEqual(missing.firstChild,failedTile);
assert.ok(missing.firstChild.querySelector('img'));
assert.equal(healthy.firstChild.firstChild,goodImage);
assert.equal(noIcon.firstChild.querySelector('img'),null);
""", native=True, setup="""
fixture.device_id='synthetic';fixture.diff.has_changes=false;fixture.history.undo=false;
fixture.library=['A','B','C'].map((name,i)=>({id:'lib.'+name,name,kind:'app',icon:i<2?'icon.'+name:null}));
""")


@pytest.fixture(params=[False, True], ids=['light', 'dark'])
def editor_contrast(request):
    css = (WEB / 'app.css').read_text()
    roots = re.findall(r':root\s*\{([^{}]*)\}', css)
    colors = dict(re.findall(r'(--[\w-]+)\s*:\s*([^;]+)', roots[0]))
    if request.param:
        colors.update(re.findall(r'(--[\w-]+)\s*:\s*([^;]+)', roots[1]))

    def declarations(selector):
        blocks = re.findall(r'(?m)^' + re.escape(selector) + r'\s*\{([^{}]*)\}', css)
        return dict(re.findall(r'([\w-]+)\s*:\s*([^;]+)', ';'.join(blocks)))

    def rgb(value):
        value = value.strip()
        if value.startswith('var('):
            return rgb(colors[value[4:-1]])
        value = value.lstrip('#')
        if len(value) == 3:
            value = ''.join(c * 2 for c in value)
        return [int(value[i:i + 2], 16) for i in (0, 2, 4)]

    def ratio(foreground, background, opacity=1):
        foreground = [a * opacity + b * (1 - opacity) for a, b in zip(foreground, background)]

        def luminance(values):
            values = [c / 255 for c in values]
            values = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in values]
            return sum(c * w for c, w in zip(values, (.2126, .7152, .0722)))

        a, b = sorted((luminance(foreground), luminance(background)))
        return (b + .05) / (a + .05)

    return declarations, rgb, ratio


@pytest.mark.parametrize('selector,surface', [
    ('.page-head .num', '--phone'), ('.page-head .slots', '--phone'),
    ('.fpage .num', '--phone'), ('.page-head .slots.over', '--phone'),
    ('.summary .problem', '--panel'), ('.problems', '--panel'),
    ('.menu .menu-foot', '--panel'), ('.menu .menu-foot', '--panel-2'),
])
def test_enabled_caption_validation_and_recovery_text_contrast(editor_contrast, selector, surface):
    declarations, rgb, ratio = editor_contrast
    foreground, background = rgb(declarations(selector)['color']), rgb('var(' + surface + ')')
    if selector == '.menu .menu-foot' and surface == '--panel-2':
        brightness = float(re.search(r'brightness\(([^)]+)\)', declarations('button:hover:not(:disabled)')['filter'])[1])
        foreground, background = [[c * brightness for c in color] for color in (foreground, background)]
    assert ratio(foreground, background) >= 4.5


@pytest.mark.parametrize('surface', ['--phone', '--panel'])
def test_search_nonmatch_names_keep_enabled_text_contrast(editor_contrast, surface):
    declarations, rgb, ratio = editor_contrast
    dim = declarations('.item.dim')
    name = declarations('.item .name') | declarations('.item.dim .name')
    opacity = float(dim.get('opacity', 1)) * float(name.get('opacity', 1))
    assert ratio(rgb(name.get('color', 'var(--fg)')), rgb('var(' + surface + ')'), opacity) >= 4.5


def test_pending_operation_text_contrasts_over_the_lightest_content(editor_contrast):
    declarations, rgb, ratio = editor_contrast
    busy = declarations('.busy')
    scrim = [float(n) for n in re.findall(r'[\d.]+', busy['background'])]
    background = [a * scrim[3] + 255 * (1 - scrim[3]) for a in scrim[:3]]
    assert ratio(rgb(busy['color']), background) >= 4.5
