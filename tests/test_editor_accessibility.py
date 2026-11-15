"""Keyboard, recovery and rendering contracts for the editor."""
from test_web_i18n import browser


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
