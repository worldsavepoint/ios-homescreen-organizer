"""Browser behavior with a synthetic DOM and intercepted device routes; Node only."""
import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "homescreen/web"


class Nodes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "parent": self.stack[-1] if self.stack else None}
        self.nodes.append(node)
        if tag not in {"meta", "link", "input", "img", "br", "hr"}:
            self.stack.append(len(self.nodes) - 1)

    def handle_endtag(self, tag):
        for pos in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[pos]]["tag"] == tag:
                self.stack = self.stack[:pos]
                break

    def handle_data(self, data):
        if self.stack:
            node = self.nodes[self.stack[-1]]
            node["text"] = node.get("text", "") + data


HARNESS = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const harnessInput = JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(tag, attrs={}) {this.tagName=tag;this.attrs={...attrs};this.children=[];this.events={};this.style={setProperty(){}};
 this.dataset={}; for(const [k,v] of Object.entries(attrs))if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=v;
 this.hidden='hidden' in attrs;this.disabled='disabled' in attrs;this.checked='checked' in attrs;this.value=attrs.value||'';this._text='';
 this._scrollHeight=1000;this._clientHeight=200;this.scrollTop=0;
 const classes = new Set((attrs.class||'').split(' '));this.classList={add:(...xs)=>xs.forEach(x=>classes.add(x)),remove:(...xs)=>xs.forEach(x=>classes.delete(x)),contains:x=>classes.has(x),toggle:(x,b)=>{b??=!classes.has(x);b?classes.add(x):classes.delete(x);}};
 }
 get layoutHidden(){for(let n=this;n;n=n.parent)if(n.hidden)return true;return false;}
 get scrollHeight(){return this.layoutHidden?0:this._scrollHeight;}set scrollHeight(v){this._scrollHeight=v;}
 get clientHeight(){return this.layoutHidden?0:this._clientHeight;}set clientHeight(v){this._clientHeight=v;}
 set className(v){this.attrs.class=v;}get className(){return this.attrs.class||'';}
 set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
 get id(){return this.attrs.id||'';}set id(v){this.attrs.id=v;}
 getAttribute(k){if(k.startsWith('data-'))return this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]??null;return this.attrs[k]??null;}removeAttribute(k){delete this.attrs[k];if(k.startsWith('data-'))delete this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())];}
 setAttribute(k,v){this.attrs[k]=String(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(v);}
 append(...xs){for(let x of xs){if(typeof x==='string')x=new Element('#text',{value:x});x.parent=this;this.children.push(x);}}
 appendChild(x){this.append(x);return x;}replaceChildren(...xs){this.children=[];this._text='';this.append(...xs);}
 get lastChild(){return this.children.at(-1);}get firstChild(){return this.children[0];}
 remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
 matches(selector){return selector.split(',').some(s=>{s=s.trim();if(s.includes(' '))return false;
 const attr=s.match(/\[([^=\]]+)(?:=["']?([^"'\]]+)["']?)?\]/);
 if(attr && (this.getAttribute(attr[1])===null || (attr[2]&&this.getAttribute(attr[1])!==attr[2])))return false;
 s=s.replace(/\[[^\]]+\]/g,'');const id=s.match(/#([\w-]+)/);if(id&&this.attrs.id!==id[1])return false;
 const cl=s.match(/\.([\w-]+)/);if(cl&&!this.classList.contains(cl[1])&&!this.className.split(' ').includes(cl[1]))return false;
 const tag=s.match(/^[\w-]+/);return !tag||this.tagName===tag[0];});}
 querySelectorAll(s){const out=[];const parts=s.trim().split(/\s+/);for(const c of this.children){if(c.matches(parts.at(-1))&&(parts.length===1||c.parent?.closest(parts.slice(0,-1).join(' '))))out.push(c);out.push(...c.querySelectorAll(s));}return out;}
 cloneNode(deep){const n=new Element(this.tagName,this.attrs);if(deep)for(const c of this.children)n.append(c.cloneNode(true));return n;}
 querySelector(s){return this.querySelectorAll(s)[0]||null;}
 closest(s){return this.matches(s)?this:this.parent?.closest(s);}contains(n){return n===this||this.children.some(c=>c.contains(n));}
 addEventListener(k,f){(this.events[k]??=[]).push(f);}removeEventListener(k,f){this.events[k]=(this.events[k]||[]).filter(x=>x!==f);}
 dispatchEvent(e){e.target??=this;e.stopPropagation??=()=>{};e.preventDefault??=()=>{e.defaultPrevented=true;};this['on'+e.type]?.(e);for(const f of this.events[e.type]||[])f(e);return true;}
 click(){if(!this.disabled)this.dispatchEvent({type:'click'});}focus(){document.activeElement=this;}select(){}blur(){document.activeElement=null;}
}
const document = new Element('document');document.createElement=t=>new Element(t);document.createTextNode=t=>{const n=new Element('#text');n.textContent=t;return n;};
const nodes=harnessInput.nodes.map(n=>new Element(n.tag,n.attrs));nodes.forEach((n,i)=>{n._text=harnessInput.nodes[i].text||'';(harnessInput.nodes[i].parent==null?document:nodes[harnessInput.nodes[i].parent]).append(n);});
document.documentElement=nodes.find(n=>n.tagName==='html');document.body=nodes.find(n=>n.tagName==='body');
const calls=[], stored=new Map(), bridge=[], copied=[], unhandled=[];
process.on('unhandledRejection',error=>unhandled.push(error.message));
const item={id:'synthetic.app',kind:'app',name:'Synthetic Notes',slots:1};
const fixture={loaded:true,read_only:true,binding:{device:'synthetic',revision:'current'},source:'phone',device:{DeviceName:'Synthetic iPhone',ProductVersion:'18.0',ProductType:'iPhone'},
 dock:[],pages:[[{id:'synthetic.folder',kind:'folder',name:'My Folder',pos:[0,0,1,1],pages:[[item]]}]],library:[],bank:[],moved:['synthetic.app'],history:{undo:true,redo:false},
 grid:{cols:4,rows:6,dock:4,folder:9},slots:{page:24},diff:{has_changes:true,apply_token:"reviewed",placement_changes:[],items_moved:1,pages_before:1,pages_after:1,gaps_changed:0,to_app_library:0,problems:[]},moves:{},usage:null};
const context={document,console,URL,URLSearchParams,Intl,Date,Set,Map,Promise,setTimeout:()=>0,clearTimeout(){},requestAnimationFrame:f=>f(),
 location:{hash:'',pathname:'/',search:''},history:{replaceState(){}},navigator:{languages:['en'],clipboard:{writeText:async t=>copied.push(t)}},
 localStorage:{getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v)},Image:class extends Element{constructor(){super('img');}},
 Event:class{constructor(type){this.type=type;}},CustomEvent:class{constructor(type){this.type=type;}},
 addEventListener:(...a)=>document.addEventListener(...a),dispatchEvent:e=>document.dispatchEvent(e),close:()=>{context.closed=true;},
 fetch:async(path,opts={})=>{calls.push(String(path));const file=String(path).slice('/static/locales/'.length);
 if(path==='/static/locales/'+file && Object.hasOwn(harnessInput.catalogs,file) && !opts.method)
   return {ok:true,json:async()=>harnessInput.catalogs[file]};
 if(path==='/api/state'&&!opts.method)return {ok:true,json:async()=>fixture};if(path==='/api/devices'&&!opts.method)return {ok:true,json:async()=>({switching:false,current:null,devices:[]})};
 if(path==='/api/read'&&opts.method==='POST')return {ok:true,json:async()=>fixture};throw Error('Unexpected route '+path);}
};context.window=context;context.globalThis=context;
if(harnessInput.native) {context.screenOrganizerHost={native:true,languagePreference:'de',consentAccepted:true};context.webkit={messageHandlers:{screenOrganizer:{postMessage:m=>bridge.push(m)}}};}
eval(harnessInput.setup || '');
vm.createContext(context);
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{for(const [index,script] of harnessInput.scripts.entries()){
 vm.runInContext(script,context);
 if(index===0 && context.waitForInitialCatalog)await context.ScreenOrganizerI18n.ready;
}
await eval('(async()=>{'+(harnessInput.before_ready || '')+'})()');if(context.expectCatalogFailure)await context.ScreenOrganizerI18n.ready.catch(e=>context.catalogFailure=e.message);
else await context.ScreenOrganizerI18n.ready;
await tick();await tick();
const $=s=>document.querySelector(s);const i18n=context.ScreenOrganizerI18n;
''' 


def browser(body, native=False, setup="", before_ready=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    parser = Nodes()
    parser.feed((WEB / "index.html").read_text())
    catalogs = {p.name: json.loads(p.read_text()) for p in (WEB / "locales").glob("*.json")}
    scripts = [(WEB / name).read_text() for name in ("i18n.js", "onboarding.js", "app.js") if (WEB / name).exists()]
    result = subprocess.run([node, "-e", HARNESS + body + "\nassert.deepEqual(unhandled, []);console.log(\"browser assertions complete\");})().catch(e=>{console.error(e);process.exitCode=1;});"],
        input=json.dumps({"nodes": parser.nodes, "scripts": scripts, "catalogs": catalogs, "native": native, "setup": setup, "before_ready": before_ready}),
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "browser assertions complete", "Browser assertions did not finish"


def test_standalone_consent_blocks_routes_until_read_and_agreed():
    browser('''
assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0,'device routes must wait for consent');
assert.equal($('#consent-dialog').hidden,false);
assert.equal($('#consent-continue').disabled,true);
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
assert.equal($('#consent-continue').disabled,true,'checkbox alone cannot pass');
const terms=$('#consent-text');terms.scrollTop=800;terms.dispatchEvent({type:'scroll'});
assert.equal($('#consent-continue').disabled,false);
let accepted=0;document.addEventListener('screen-organizer-consent-accepted',()=>accepted++);
$('#consent-continue').click();await tick();await tick();
assert.equal(accepted,1);assert.equal($('#consent-dialog').hidden,true);
assert.ok(calls.includes('/api/state'));assert.ok(calls.includes('/api/devices'));
''')


def test_decline_keeps_device_routes_blocked():
    browser('''
assert.ok($('#consent-decline'),'decline control is present');$('#consent-decline').click();await tick();
assert.equal(context.closed,true);assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0);
''')


def test_native_language_switch_preserves_draft_search_folder_and_selection():
    browser('''
assert.equal(i18n.locale,'de');assert.equal($('#consent-dialog').hidden,true);
assert.equal($('#reset').textContent,'Zurücksetzen');
$('#pages').querySelector('[data-id="synthetic.folder"]').click();
$('#folder-pages').querySelector('[data-id="synthetic.app"]').click();
$('#search').value='Synthetic';$('#search').dispatchEvent({type:'input'});
const before=calls.length;const draft=vm.runInContext('V',context);
$('#language-select').value='fr';$('#language-select').dispatchEvent({type:'change'});await tick();await tick();
assert.equal($('#reset').textContent,'Réinitialiser');
assert.equal($('#search').value,'Synthetic');assert.equal($('#folder-name').value,'My Folder');assert.equal($('#drawer').hidden,false);
assert.ok($('#folder-pages').querySelector('[data-id="synthetic.app"]').classList.contains('selected'));
assert.equal(vm.runInContext('V',context),draft);assert.equal(calls.length,before+1,'only the selected catalog is fetched');
assert.deepEqual(JSON.parse(JSON.stringify(bridge.at(-1))),{action:'setLanguage',preference:'fr'});
''', native=True)


def test_mcp_entry_uses_native_bridge_and_browser_copyable_setup():
    browser('''
assert.ok($('#mcp-setup'),'toolbar setup entry');$('#mcp-setup').click();
assert.deepEqual(JSON.parse(JSON.stringify(bridge.at(-1))),{action:'openMCPSetup'});
''', native=True)
    browser('''
const terms=$('#consent-text');terms.scrollTop=800;terms.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});$('#consent-continue').click();await tick();
$('#mcp-setup').click();assert.equal($('#mcp-dialog').hidden,false);
assert.equal(document.activeElement.id,'mcp-title','setup opens at its instructions');
const text=$('#mcp-dialog').textContent;assert.ok(text.includes('home-screen-mcp'));assert.ok(text.includes('HOMESCREEN_READ_ONLY'));
$('#mcp-copy-json').click();await tick();
const config=JSON.parse(copied[0]);assert.equal(config.mcpServers['ios-homescreen-mcp'].command,'home-screen-mcp');
assert.equal(config.mcpServers['ios-homescreen-mcp'].env.HOMESCREEN_READ_ONLY,'1');
''')


def test_view_adds_name_safe_current_original_locations_and_validation(monkeypatch):
    from types import SimpleNamespace
    from homescreen.model import Layout
    import homescreen.ui as ui
    from fake_phone import app, folder

    name = 'page "dock" -> folder [1] \'quoted\''
    app_name = 'dock -> page [Synthetic]'
    lay = Layout([[], [folder(name, [app('synthetic.app', app_name)])]], library=[])
    old = lay.positions()['synthetic.app']
    lay.move('synthetic.app', 'bank')
    session = SimpleNamespace(layout=lay, source=SimpleNamespace(name='synthetic'), device_id=None,
                              info={}, usage=None, gaps_readable=False, history=lambda: {'undo': True, 'redo': False})
    monkeypatch.setattr(ui, 'session', lambda: session)
    result = ui.view()
    assert result['moves'] == lay.diff()['moves']
    assert result['diff']['problems'] == lay.validate()
    assert result['location_labels'][old] == {'kind': 'folder', 'name': name, 'page': 1, 'index': 1}
    assert result['location_labels']['App Bank'] == {'kind': 'bank'}
    detail = next(iter(result['move_details'].values()))
    assert detail == {'id': 'synthetic.app', 'name': app_name,
                      'from': {'kind': 'folder', 'name': name, 'page': 1, 'index': 1}, 'to': {'kind': 'bank'}}
    assert result['diff']['problem_details'][0] == {'key': 'validation.bank', 'params': {'count': 1}}


def test_view_validation_keeps_quoted_folder_names_as_parameters(monkeypatch):
    from types import SimpleNamespace
    from homescreen.model import Layout
    import homescreen.ui as ui
    from fake_phone import app, folder

    name = 'dock "page" \'folder\' -> [x]'
    apps = [app(f'synthetic.{i}', f'App {i}') for i in range(10)]
    raw_folder = folder(name, [])
    raw_folder['iconLists'] = [apps]
    lay = Layout([[], [raw_folder]], library=[])
    monkeypatch.setattr(ui, 'session', lambda: SimpleNamespace(layout=lay, source=SimpleNamespace(name='synthetic'),
        device_id=None, info={}, usage=None, gaps_readable=False, history=lambda: {}))
    result = ui.view()
    assert result['diff']['problem_details'] == [{'key': 'validation.folderCapacity',
        'params': {'name': name, 'page': 1, 'count': 10, 'capacity': 9}}]


def test_open_prompt_translates_without_changing_typed_folder_name():
    browser('''
$('#pages').querySelector('[data-id="synthetic.folder"]').click();
$('#folder-pages').querySelector('[data-id="synthetic.app"]').click();
$('#sel-folder').click();await tick();
const input=$('#modal-body').querySelector('input');input.value='dock -> page [My Name]';
await i18n.setLocale('fr');await tick();
assert.equal($('#modal-title').textContent,'Nouveau dossier');
assert.equal($('#modal-actions').firstChild.textContent,'Annuler');
assert.equal(input.value,'dock -> page [My Name]');assert.equal($('#modal').hidden,false);
''', native=True)


def test_usage_and_review_translate_structured_locations_and_validation():
    browser('''
fixture.read_only=false;fixture.diff.problems=['the App Bank still holds 1 item(s); place them or send them to the App Library'];
fixture.diff.problem_details=[{key:'validation.bank',params:{count:1}}];
const name='page "dock" -> folder [User]';
fixture.location_labels={'opaque location':{kind:'folder',name,page:2,index:3}};
fixture.move_details={'legacy move':{name,id:'synthetic.app',from:{kind:'page',page:1,index:1},to:{kind:'folder',name,page:2,index:3}}};
fixture.moves={'legacy move':'opaque legacy string'};
fixture.usage={ok:true,days:7,source:'mac',original:1234,now:1230,top:[{id:'synthetic.app',name:'Synthetic Notes',opens:1234,where:'opaque location'}]};
await i18n.setLocale('fr');await tick();
assert.ok($('#usage').textContent.includes('Dossier « '+name+' »'));
$('#review').click();await tick();
assert.ok($('#modal-body').textContent.includes('La réserve contient 1 éléments'));
assert.ok($('#modal-body').textContent.includes(name));assert.ok(!$('#modal-body').textContent.includes('opaque legacy string'));
''', native=True)


def test_known_errors_show_localized_state_and_expandable_original_diagnostic():
    browser('''
context.fetch=async(path,opts)=>{assert.equal(path,'/api/op');assert.equal(opts.method,'POST');
 return {ok:false,status:400,json:async()=>({error:"widgets can't go in the dock"})};};
$('#undo').click();await tick();await tick();
assert.ok($('#toast').textContent.includes('Widgets können nicht im Dock platziert werden.'));
assert.ok($('#toast').querySelector('details'),'original diagnostic is expandable');
assert.ok($('#toast').querySelector('pre').textContent.includes("widgets can't go in the dock"));
''', native=True)


@pytest.mark.parametrize("message,key", [
    ("Read-only mode doesn't fit at row 6, column 1", "error.position"),
    ("the saved draft changed in another session; reopen it or read the device again", "error.staleDraft"),
    ("the device placement changed in another session; read the device again", "error.readAgain"),
    ("couldn't read the device: device grid metrics must be positive integers; read the device again", "error.readAgain"),
    ("keep a Home Screen page to preserve this archive's grid dimensions", "error.keepPage"),
    ("the USB root archive uses one display identifier for different apps", "error.wireConflict"),
    ("device grid changed during read; no layout was loaded", "error.readAgain"),
    ("device grid changed; explicitly read the intended device again", "error.readAgain"),
    ("saved draft identity is invalid; call read again", "error.readAgain"),
    ("saved draft pin identity is unavailable; call read again", "error.readAgain"),
    ("saved draft pin identity is ambiguous; call read again", "error.readAgain"),
    ("ambiguous pin identifier on a layout page", "error.ambiguousIdentifier"),
    ("ambiguous pin identifier on a backup page", "error.ambiguousIdentifier"),
    ("pins must identify items on each page", "error.savedPosition"),
    ("invalid saved pin memory", "error.savedPosition"),
    ("invalid saved pin memory; read again", "error.readAgain"),
    ("list-format writes cannot send fixed positions; use ordinary page order", "error.writeFormat"),
    ("exported pin metadata has invalid page identifiers", "error.readAgain"),
    ("saved pin footprint does not fit the loaded grid", "error.savedPosition"),
    ("widget 'name with quotes' has no known wire identifier; use ordinary page order", "error.pinIdentifier"),
    ("folder 'name' has no wire identifier for its pin; use ordinary page order", "error.pinIdentifier"),
    ("pinned backup has no saved grid dimensions; use a backup with grid provenance", "error.backupGrid"),
    ("backup grid dimensions must be positive integers", "error.backupGrid"),
    ("backup pins must identify items on each page", "error.savedPosition"),
    ("backup pin is outside its saved grid or does not identify an item on its page", "error.savedPosition"),
    ("backup pin footprint does not fit the saved or loaded grid", "error.savedPosition"),
    ("invalid pin cell for 'example' on the loaded grid", "error.savedPosition"),
    ("pin identifiers do not match the layout's page items", "error.savedPosition"),
    ("pins do not fit the loaded grid: example", "error.savedPosition"),
    ("backup layout has problems, not restoring: ['page 1: no room for example.a24']", "error.savedPosition"),
])
def test_pin_integrity_errors_have_localized_primary_messages(message, key):
    browser(f'''
const message={json.dumps(message)}, key={json.dumps(key)};
context.fetch=async(path,opts)=>{{assert.equal(path,'/api/op');assert.equal(opts.method,'POST');
 return {{ok:false,status:400,json:async()=>({{error:message}})}};}};
$('#undo').click();await tick();await tick();
assert.notEqual(i18n.t(key),key);
assert.ok($('#toast').textContent.startsWith(i18n.t(key)));
assert.equal($('#toast').querySelector('pre').textContent,message);
''', native=True)


def test_setup_escape_preserves_editor_selection_and_open_folder():
    browser('''
// Browser setup uses the same escape path as a native host's web panel.
const terms=$('#consent-text');terms.scrollTop=800;terms.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});$('#consent-continue').click();await tick();await tick();
$('#pages').querySelector('[data-id="synthetic.folder"]').click();
$('#folder-pages').querySelector('[data-id="synthetic.app"]').click();
$('#mcp-setup').click();document.dispatchEvent({type:'keydown',key:'Escape',target:$('#mcp-title')});
assert.equal($('#mcp-dialog').hidden,true);assert.equal($('#drawer').hidden,false);
assert.ok($('#folder-pages').querySelector('[data-id="synthetic.app"]').classList.contains('selected'));
''')


def test_widget_size_and_usage_recovery_instructions_use_selected_language():
    browser('''
fixture.pages[0].push({id:'synthetic.widget',kind:'widget',name:'My medium Widget',size:'medium',slots:8});
fixture.usage={ok:false,error:'No synced Screen Time data on this Mac. Turn on Screen Time > Share Across Devices on both the iPhone and the Mac (same Apple Account).'};
await i18n.setLocale('fr');await tick();
assert.ok($('#pages').querySelector('[data-id="synthetic.widget"]').textContent.includes('Moyen'));
assert.ok($('#pages').querySelector('[data-id="synthetic.widget"]').textContent.includes('My medium Widget'));
assert.ok($('#usage').textContent.includes('Activez Temps d’écran'));
''', native=True)


def test_language_change_requires_reading_translated_agreement():
    browser('''
const text=$('#consent-text');text.scrollTop=800;text.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
assert.equal($('#consent-continue').disabled,false);
$('#consent-language-select').value='fr';$('#consent-language-select').dispatchEvent({type:'change'});await tick();await tick();
assert.equal($('#consent-continue').disabled,true);assert.equal(text.scrollTop,0);
assert.equal($('#consent-title').textContent,'Avant de commencer');
assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0);
''')


def test_backup_dates_follow_locale_without_loading_a_new_view():
    browser('''
const originalFetch=context.fetch;
context.fetch=async path=>path==='/api/backups'?{ok:true,json:async()=>({binding:fixture.binding,backups:['20261113-162400-before-apply-00000000000000000000000000000000.plist','20261113-162400-synthetic-phone.plist']})}:originalFetch(path);
$('#backups-btn').click();await tick();await tick();
assert.ok($('#modal-body').textContent.includes('13.11.2026'));
assert.ok($('#modal-body').textContent.includes('20261113-162400-synthetic-phone.plist'));
assert.ok(!$('#modal-body').textContent.includes('before apply'));
await i18n.setLocale('fr');await tick();
assert.ok($('#modal-body').textContent.includes('13/11/2026'));
assert.equal($('#modal-title').textContent,'Sauvegardes');
''', native=True)


def test_view_move_details_keep_original_and_renamed_folder_positions(monkeypatch):
    from types import SimpleNamespace
    from homescreen.model import Layout
    import homescreen.ui as ui
    from fake_phone import app, folder

    old_name = 'old page -> dock [folder]'
    new_name = 'new "folder" -> [page]'
    lay = Layout([[], [app('synthetic.a', 'page -> [A]'), folder(old_name, [app('synthetic.b', 'dock [B]')])],
                  [app('synthetic.c', 'folder C')]], library=[])
    folder_id = next(node.id for node in lay.pages[0] if node.kind == 'folder')
    old_b = lay.positions()['synthetic.b']
    lay.move('synthetic.a', f'{folder_id}:1')
    lay.move('synthetic.b', 'page:2')
    lay.rename_folder(folder_id, new_name)
    monkeypatch.setattr(ui, 'session', lambda: SimpleNamespace(layout=lay, source=SimpleNamespace(name='synthetic'),
        device_id=None, info={}, usage=None, gaps_readable=False, history=lambda: {}))
    result = ui.view()
    details = {detail['id']: detail for detail in result['move_details'].values()}
    assert details['synthetic.a']['name'] == 'page -> [A]'
    assert details['synthetic.a']['from'] == {'kind': 'page', 'page': 1, 'index': 1}
    assert details['synthetic.a']['to'] == {'kind': 'folder', 'name': new_name, 'page': 1, 'index': 1}
    assert details['synthetic.b']['from'] == {'kind': 'folder', 'name': old_name, 'page': 1, 'index': 1}
    assert details['synthetic.b']['to'] == {'kind': 'page', 'page': 2, 'index': 2}
    assert result['location_labels'][old_b]['name'] == old_name
    assert result['moves'] == lay.diff()['moves']


def test_pending_language_immediately_blocks_consent_until_new_terms_are_read():
    browser('''
const text=$('#consent-text');text.scrollTop=800;text.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
assert.equal($('#consent-continue').disabled,false);
const originalFetch=context.fetch;let release;
context.fetch=path=>String(path).endsWith('/fr.json')?new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>harnessInput.catalogs['fr.json']});}):originalFetch(path);
const select=$('#consent-language-select');select.value='fr';const changing=select.onchange({target:select});
assert.equal($('#consent-continue').disabled,true,'language selection blocks consent synchronously');
$('#consent-continue').onclick();await tick();
text.scrollTop=800;text.dispatchEvent({type:'scroll'});$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
$('#consent-continue').onclick();await tick();
assert.equal(context.ScreenOrganizerOnboarding.accepted,false);assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0);
text.scrollHeight=1200;release();await changing;await tick();
assert.equal($('#consent-title').textContent,'Avant de commencer');assert.equal($('#consent-agree').checked,false);
assert.equal(text.scrollTop,0);assert.equal($('#consent-continue').disabled,true);
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
assert.equal($('#consent-continue').disabled,true);
text.scrollTop=1000;text.dispatchEvent({type:'scroll'});$('#consent-continue').click();await tick();await tick();
assert.equal(context.ScreenOrganizerOnboarding.accepted,true);assert.ok(calls.includes('/api/devices'));
''')


@pytest.mark.parametrize("failure", ["network", "http"])
def test_failed_language_load_keeps_consent_blocked_until_fallback_terms_are_read(failure):
    browser('''
const text=$('#consent-text');text.scrollTop=800;text.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
const originalFetch=context.fetch;let fail;
context.fetch=path=>String(path).endsWith('/fr.json')?new Promise((resolve,reject)=>{fail=()=>FAILURE==='network'?reject(Error('synthetic network failure')):resolve({ok:false});}):originalFetch(path);
const select=$('#consent-language-select');select.value='fr';const changing=select.onchange({target:select});
assert.equal($('#consent-continue').disabled,true);await tick();
$('#consent-continue').onclick();assert.equal(context.ScreenOrganizerOnboarding.accepted,false);
fail();await changing;await tick();
assert.equal(document.documentElement.lang,'en');assert.equal(i18n.preference,'fr');
assert.equal($('#consent-title').textContent,'Before you begin');assert.equal($('#consent-agree').checked,false);assert.equal(text.scrollTop,0);
context.fetch=originalFetch;await i18n.setLocale('fr');assert.equal(document.documentElement.lang,'fr');
assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0);
text.scrollTop=800;text.dispatchEvent({type:'scroll'});$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
$('#consent-continue').click();await tick();await tick();assert.ok(calls.includes('/api/devices'));
'''.replace('FAILURE', json.dumps(failure)))


@pytest.mark.parametrize("completion_order", ["old_first", "latest_first"])
def test_superseded_language_load_cannot_change_latest_consent_eligibility(completion_order):
    browser('''
const text=$('#consent-text');text.scrollTop=800;text.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
const originalFetch=context.fetch;const release={};
context.fetch=path=>['fr.json','de.json'].includes(String(path).split('/').pop())?new Promise(resolve=>{const file=String(path).split('/').pop();release[file]=()=>resolve({ok:true,json:async()=>harnessInput.catalogs[file]});}):originalFetch(path);
const select=$('#consent-language-select');select.value='fr';const old=select.onchange({target:select});await tick();
select.value='de';const latest=select.onchange({target:select});await tick();
assert.equal($('#consent-continue').disabled,true);
if(ORDER==='old_first') {release['fr.json']();await old;assert.equal($('#consent-continue').disabled,true);$('#consent-continue').onclick();}
assert.equal(context.ScreenOrganizerOnboarding.accepted,false);assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0);
release['de.json']();await latest;await tick();assert.equal($('#consent-title').textContent,'Bevor du beginnst');
assert.equal($('#consent-agree').checked,false);text.scrollTop=800;text.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});assert.equal($('#consent-continue').disabled,false);
if(ORDER==='latest_first') {release['fr.json']();await old;assert.equal($('#consent-continue').disabled,false);assert.equal($('#consent-agree').checked,true);}
$('#consent-continue').click();await tick();await tick();assert.ok(calls.includes('/api/devices'));
'''.replace('ORDER', json.dumps(completion_order)))


@pytest.mark.parametrize("failure", ["expired", "network"])
def test_bootstrap_auth_failure_is_handled_before_consent_and_localized_after_catalogs(failure):
    browser('''
assert.equal(context.location.hash,'');assert.deepEqual(unhandled,[]);
assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0);
assert.ok(!document.body.textContent.includes('editor.authenticationFailed'));
const text=$('#consent-text');text.scrollTop=800;text.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});$('#consent-continue').click();await tick();await tick();
assert.equal($('#toast').hidden,false);
assert.ok($('#toast').textContent.includes('Authentification impossible. Rouvrez Screen Organizer depuis son lanceur.'));
assert.ok(!document.body.textContent.includes('editor.authenticationFailed'));
assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0,'failed authentication blocks device routes');
''', setup='''
stored.set('screenOrganizer.language','fr');context.location.hash='#token=synthetic-expired';
context.history.replaceState=()=>{calls.push('fragment-cleared');context.location.hash='';};
const originalFetch=context.fetch;
context.fetch=(path,options)=>{
 if(path==='/v1/authenticate'){calls.push(path);assert.equal(context.location.hash,'');return FAILURE==='network'?Promise.reject(Error('synthetic authentication network failure')):Promise.resolve({ok:false,status:401});}
 if(String(path).endsWith('/en.json'))return new Promise(resolve=>{context.releaseCatalog=()=>resolve({ok:true,json:async()=>harnessInput.catalogs['en.json']});});
 return originalFetch(path,options);
};
'''.replace('FAILURE', json.dumps(failure)), before_ready='''
await tick();await tick();assert.deepEqual(unhandled,[],'bootstrap failure is handled while catalogs and consent wait');
assert.equal(context.location.hash,'');assert.deepEqual(calls.slice(0,3),['/static/locales/manifest.json','fragment-cleared','/v1/authenticate']);
context.releaseCatalog();
''')


def test_initial_hidden_terms_require_reading_the_visible_scroll_end():
    browser('''
const text=$('#consent-text');assert.equal($('#consent-dialog').hidden,false);assert.equal(text.scrollTop,0);
assert.ok(text.scrollHeight>text.clientHeight);
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
assert.equal($('#consent-continue').disabled,true,'checkbox alone cannot accept initially hidden overflowing terms');
$('#consent-continue').onclick();await tick();assert.equal(context.ScreenOrganizerOnboarding.accepted,false);
assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0);
text.scrollTop=400;text.dispatchEvent({type:'scroll'});assert.equal($('#consent-continue').disabled,true);
text.scrollTop=800;text.dispatchEvent({type:'scroll'});$('#consent-continue').click();await tick();await tick();
assert.ok(calls.includes('/api/devices'));
''', setup='''
const terms=nodes.find(n=>n.id==='consent-text');assert.equal(terms.clientHeight,0);assert.equal(terms.scrollHeight,0);
''')


def test_long_translated_terms_require_the_new_visible_scroll_end():
    browser('''
const text=$('#consent-text');text.scrollTop=800;text.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});assert.equal($('#consent-continue').disabled,false);
const originalFetch=context.fetch;
context.fetch=async path=>{
 if(String(path).endsWith('/fr.json')){text.scrollHeight=1800;return {ok:true,json:async()=>({...harnessInput.catalogs['fr.json'],'consent.backups':harnessInput.catalogs['fr.json']['consent.backups'].repeat(5)})};}
 return originalFetch(path);
};
const select=$('#consent-language-select');select.value='fr';await select.onchange({target:select});await tick();
assert.equal(text.scrollTop,0);assert.equal($('#consent-agree').checked,false);assert.equal(text.scrollHeight,1800);
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
assert.equal($('#consent-continue').disabled,true);$('#consent-continue').onclick();assert.equal(context.ScreenOrganizerOnboarding.accepted,false);
text.scrollTop=800;text.dispatchEvent({type:'scroll'});assert.equal($('#consent-continue').disabled,true,'the old text end does not complete longer terms');
assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0);
text.scrollTop=1600;text.dispatchEvent({type:'scroll'});$('#consent-continue').click();await tick();await tick();assert.ok(calls.includes('/api/devices'));
''')


def test_zero_layout_terms_stay_blocked_until_they_are_rendered_and_read():
    browser('''
const text=$('#consent-text');assert.equal($('#consent-dialog').hidden,false);
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
assert.equal($('#consent-continue').disabled,true,'zero layout cannot complete reading');
$('#consent-continue').onclick();assert.equal(context.ScreenOrganizerOnboarding.accepted,false);
text.clientHeight=200;text.scrollHeight=1000;$('#consent-agree').dispatchEvent({type:'change'});
assert.equal($('#consent-continue').disabled,true);
text.scrollTop=800;text.dispatchEvent({type:'scroll'});assert.equal($('#consent-continue').disabled,false);
''', setup='''
const text=nodes.find(n=>n.id==='consent-text');text.clientHeight=0;text.scrollHeight=0;
''')


def test_fully_visible_terms_allow_agreement_without_extra_scrolling():
    browser('''
const text=$('#consent-text');assert.equal(text.clientHeight,200);assert.equal(text.scrollHeight,200);assert.equal(text.scrollTop,0);
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
assert.equal($('#consent-continue').disabled,false);$('#consent-continue').click();await tick();await tick();assert.ok(calls.includes('/api/devices'));
''', setup='''
const text=nodes.find(n=>n.id==='consent-text');text.scrollHeight=200;
''')


@pytest.mark.parametrize("explicit", [True, False])
def test_os_language_during_pending_request_and_return_to_english(explicit):
    browser('''
const terms=$('#consent-text'), agree=$('#consent-agree'), go=$('#consent-continue');
terms.scrollTop=800;terms.dispatchEvent({type:'scroll'});agree.checked=true;agree.onchange();
let release;const originalFetch=context.fetch;
context.fetch=p=>p.endsWith('/fr.json')?new Promise(resolve=>{release=()=>originalFetch(p).then(resolve);}):originalFetch(p);
const select=$('#consent-language-select');let pending;
if(EXPLICIT){select.value='fr';pending=select.onchange({target:select});}
else{context.navigator.languages=['fr'];context.dispatchEvent({type:'languagechange'});}
assert.equal(go.disabled,true);go.onclick();
context.navigator.languages=['de'];context.dispatchEvent({type:'languagechange'});await tick();
if(EXPLICIT){release();await pending;assert.equal(i18n.preference,'fr');assert.equal(i18n.locale,'fr');}
else{await i18n.setLocale('en');release();await tick();assert.equal(i18n.locale,'en');assert.equal(select.value,'en');}
assert.equal(calls.filter(p=>p.startsWith('/api/')).length,0);assert.equal(agree.checked,false);
'''.replace('EXPLICIT', json.dumps(explicit)))


def test_host_acknowledgment_and_large_review_reuse_icon_tree_and_formatters():
    browser('''
const select=$('#language-select');select.value='fr';await select.onchange({target:select});
const page=$('#pages').firstChild, images=context.images, formats=context.formats;
await i18n.setLocale('fr');assert.equal($('#pages').firstChild,page);assert.equal(context.images,images);
assert.equal(bridge.filter(m=>m.action==='setLanguage').length,1);
for(let i=0;i<240;i++){const k='item'+i;fixture.moves[k]='';
 (fixture.move_details??={})[k]={name:k,from:{kind:'page',index:i,page:1},to:{kind:'page',index:i+1,page:2}};}
fixture.read_only=false;vm.runInContext('render();review()',context);
assert.ok($('#modal-body').textContent.includes(i18n.formatNumber(240)));
assert.ok(context.formats-formats<=1);assert.equal(i18n.formatNumber(1234),new Intl.NumberFormat('fr').format(1234));
''', native=True, setup='''
context.images=0;context.formats=0;context.Image=class extends Element{constructor(){super('img');context.images++;}};
fixture.pages[0][0].pages[0][0].icon='synthetic.app';
context.Intl=Object.create(Intl);context.Intl.NumberFormat=class extends Intl.NumberFormat{constructor(...args){super(...args);context.formats++;}};
''')


def test_unknown_outcome_and_explicit_comparison():
    browser('''
const display=r=>vm.runInContext('showResult('+JSON.stringify(r)+')',context);
display({ok:false,status:'unknown',error:'synthetic readback',backup:'recovery.plist'});
assert.ok($('#modal-body').textContent.includes(i18n.t('editor.resultUnknown')));
assert.ok($('#modal-body').textContent.includes('recovery.plist'));
assert.ok(!$('#modal-body').textContent.includes('{}'));$('#modal-actions').firstChild.click();
display({ok:false,mismatches:{added_by_ios:['synthetic.x']}});
assert.ok($('#modal-body').textContent.includes(i18n.t('editor.resultMismatch',{device:'iPhone'})));
assert.ok(!$('#modal-body').textContent.includes('undefined'));
''', native=True)


def test_editor_clears_selection_and_drawer_only_after_success():
    browser('''
const choose=()=>{$('#pages').querySelector('[data-id="synthetic.folder"]').click();
 $('#folder-pages').querySelector('[data-id="synthetic.app"]').click();};
choose();let fail=true;context.fetch=async(path,opts)=>{
 assert.equal(path,'/api/op');assert.equal(opts.method,'POST');
 return {ok:!fail,json:async()=>fail?{error:'synthetic failure'}:fixture};};
const drop=()=>vm.runInContext("dropOnto(findItem('synthetic.folder'),['synthetic.app'],{})",context);
await drop();assert.equal($('#selection-bar').hidden,false);fail=false;await drop();assert.equal($('#selection-bar').hidden,true);
assert.ok(!$('#folder-pages').querySelector('[data-id="synthetic.app"]').classList.contains('selected'));
choose();vm.runInContext('setView({loaded:false})',context);assert.equal($('#drawer').hidden,true);
assert.equal($('#selection-bar').hidden,true);assert.equal($('#folder-pages').children.length,0);
''', native=True)


def test_verified_result_keeps_backup_scope_and_pending_clean_result():
    browser('''
const show=body=>vm.runInContext(body,context);
show("showWriteOutcome({view:V,result:{ok:true,status:'verified',backup:'opaque-{backup}.plist'}},()=>t('editor.restored'))");
assert.ok($('#toast').textContent.includes('opaque-{backup}.plist'));
assert.ok($('#toast').textContent.includes(i18n.t('editor.verificationScope')));
show("showError(new Error('earlier diagnostic'))");
$('#toast').querySelector('details').open=true;
show("showWriteOutcome({view:V,result:{ok:true,status:'verified',backup:'later.plist'}},()=>t('editor.restored'))");
assert.ok($('#toast').textContent.includes('earlier diagnostic'));
assert.ok($('#operation-status').textContent.includes('later.plist'),'latest outcome includes recovery access');
$('#toast').querySelector('button').click();
assert.ok($('#toast').textContent.includes('later.plist'));
assert.ok($('#toast').textContent.includes(i18n.t('editor.restored')));
assert.ok(document.body.contains(document.activeElement));
''', native=True)


def test_result_distinguishes_matched_readback_from_failed_local_save():
    browser('''
vm.runInContext("showResult({ok:false,status:'unknown',readback:'matched',local_adoption:'failed',recovery:'read',backup:'save.plist',write_error:'TimeoutError',error:'Write returned TimeoutError. Readback matched. Saving failed.'})",context);
const body=$('#modal-body').textContent;
assert.ok(body.includes(i18n.t('editor.resultLocalSave')));
assert.ok(body.includes(i18n.t('editor.recoveryRead')));
assert.ok(body.includes(i18n.t('editor.verificationScope')));
assert.ok(body.includes('save.plist') && body.includes('TimeoutError'));
assert.ok(!body.includes(i18n.t('editor.resultMismatch',{device:'iPhone'})));
''', native=True)


def test_discovery_failure_retains_devices_and_offers_retry_without_switching():
    browser('''
let mode='complete', switches=0;
context.fetch=async(path,options={})=>{
 assert.equal(path,'/api/devices');assert.equal(options.method,undefined);
 if(mode==='http')throw Error('synthetic refresh failure');
 return {ok:true,json:async()=>({switching:true,current:'known',discovery:mode,devices:[{udid:'known',name:'Known {name}',connected:mode==='complete'?true:null,draft:false}]})};
};
await vm.runInContext('loadDevices()',context);
mode='unavailable';await vm.runInContext('loadDevices()',context);
assert.ok($('#device-menu').textContent.includes(i18n.t('editor.discoveryUnavailable')));
assert.ok($('#device-menu').textContent.includes(i18n.t('editor.connectionUnknown')));
assert.ok(!$('#device-menu').textContent.includes(i18n.t('editor.notConnected')));
mode='http';await vm.runInContext('loadDevices()',context);
assert.ok($('#device-menu').textContent.includes('Known {name}'));
assert.equal(vm.runInContext('DEV.current',context),'known');
assert.equal(switches,0);
mode='complete';await $('#device-menu').querySelector('.menu-foot').onclick({stopPropagation(){}});
await tick();assert.ok(!$('#device-menu').textContent.includes(i18n.t('editor.discoveryUnavailable')));
''', native=True)


def test_fixed_source_button_has_no_popup_or_tab_action():
    browser('''
assert.equal($('#device-btn').disabled,true);
assert.equal($('#device-btn').getAttribute('aria-haspopup'),null);
assert.equal($('#device-btn').getAttribute('tabindex'),'-1');
''', native=True)


def test_keyboard_item_controls_preserve_selection_modifiers_and_focus():
    browser('''
const key=(node,key,extra={})=>node.dispatchEvent({type:'keydown',key,...extra});
let folder=$('#pages').querySelector('[data-id="synthetic.folder"]');
assert.equal(folder.getAttribute('role'),'button');assert.equal(folder.getAttribute('aria-expanded'),'false');
folder.focus();key(folder,'Enter');
assert.equal($('#drawer').hidden,false);
let item=$('#folder-pages').querySelector('[data-id="synthetic.app"]');
assert.equal(item.getAttribute('aria-label'),'Synthetic Notes');
item.focus();key(item,' ');
item=$('#folder-pages').querySelector('[data-id="synthetic.app"]');
assert.equal(item.getAttribute('aria-pressed'),'true');assert.equal(document.activeElement,item);
key(item,'Enter',{ctrlKey:true});
item=$('#folder-pages').querySelector('[data-id="synthetic.app"]');
assert.equal(item.getAttribute('aria-pressed'),'false');assert.equal(document.activeElement,item);
await i18n.setLocale('fr');
assert.equal(document.activeElement,$('#folder-pages').querySelector('[data-id="synthetic.app"]'));
$('#drawer-close').click();
assert.equal(document.activeElement,$('#pages').querySelector('[data-id="synthetic.folder"]'));
assert.equal($('#pages').querySelector('.add-page').tagName,'button');
''', native=True)


def test_folder_prompt_has_separate_name_and_description():
    browser('''
$('#pages').querySelector('[data-id="synthetic.folder"]').click();
$('#folder-pages').querySelector('[data-id="synthetic.app"]').click();
$('#sel-folder').click();await tick();
const input=$('#modal-body').querySelector('input');
assert.equal(input.getAttribute('aria-label'),i18n.t('editor.folderName'));
assert.ok(input.getAttribute('aria-describedby'));
const description=$('#'+input.getAttribute('aria-describedby'));
assert.ok(description.textContent.includes('1'));
input.value='Raw 新 {name}';await i18n.setLocale('es');
assert.equal(input.value,'Raw 新 {name}');assert.equal(input.getAttribute('aria-label'),'Nombre de carpeta');
''', native=True)


def test_ios16_remedies_are_distinct_and_keep_original_details():
    browser('''
await i18n.setLocale('es');
vm.runInContext("showError(new Error('iOS 16 does not preserve fixed icon positions; use ordinary page order'))",context);
assert.ok($('#toast').textContent.includes(i18n.t('error.ios16')));
$('#toast').querySelector('button').click();
vm.runInContext("showError(new Error('iOS 16 keeps these widgets ahead of folders; place the folder after the widgets'))",context);
assert.ok($('#toast').textContent.includes(i18n.t('error.ios16FolderWidgets')));
assert.ok($('#toast').textContent.includes('place the folder after the widgets'));
assert.notEqual(i18n.t('error.ios16FolderWidgets'),'error.ios16FolderWidgets');
''', native=True)


def test_library_reuses_collator_and_refreshes_on_effective_locale_change():
    browser('''
const order=()=>$('#library').children.map(n=>n.dataset.id);
assert.deepEqual(order(),['a','b','c']);assert.equal(context.collators,1);
vm.runInContext('render();render()',context);assert.equal(context.collators,1);
await i18n.setLocale('fr');assert.equal(context.collators,2);
await i18n.setLocale('fr');vm.runInContext('render()',context);assert.equal(context.collators,2);
''', native=True, setup='''
fixture.library=[{id:'c',kind:'app',name:'Zèbre'},{id:'b',kind:'app',name:'École'},{id:'a',kind:'app',name:'Able'}];
context.collators=0;context.Intl=Object.create(Intl);
context.Intl.Collator=class extends Intl.Collator{constructor(...args){super(...args);context.collators++;}};
''')


def test_help_and_count_labels_follow_current_editor_options():
    browser('''
for(const code of i18n.languages.map(l=>l.code)){
 await i18n.setLocale(code);$('#help').click();
 assert.ok($('#modal-body').textContent.includes('200'),code+' undo capacity');
 $('#modal-actions').firstChild.click();
}
for(const [code,selected,folder] of [['en','Selection: 1','Create a folder. Selection: 1.'],
 ['es','Selección: 1','Crear una carpeta. Selección: 1.'],['pt','Seleção: 1','Criar uma pasta. Seleção: 1.']]){
 await i18n.setLocale(code);
 $('#pages').querySelector('[data-id="synthetic.folder"]').click();
 $('#folder-pages').querySelector('[data-id="synthetic.app"]').click();
 assert.equal($('#sel-count').textContent,selected);
 $('#sel-folder').click();await tick();
 assert.ok($('#modal-body').textContent.includes(folder));$('#modal-actions').firstChild.click();
 assert.ok(i18n.t('editor.selected',{count:2}).includes('2'));
}
''', native=True)


def view_for(monkeypatch, layout, usage=None):
    from types import SimpleNamespace
    import homescreen.ui as ui
    monkeypatch.setattr(ui, 'session', lambda: SimpleNamespace(layout=layout, source=SimpleNamespace(name='fixture'),
        device_id=None, info={}, usage=usage, gaps_readable=False, history=lambda: {}))
    return ui.view()


@pytest.mark.parametrize("kind", ["folder", "widget"])
def test_web_diff_for_container_moves_and_pins(monkeypatch, kind):
    from homescreen.model import Layout
    from fake_phone import app, folder, widget

    raw = folder("Folder", [app("child", "Child")]) if kind == "folder" else widget("Widget", "small")
    lay = Layout([[], [app("a", "A"), raw], [app("b", "B")]], library=[])
    item = lay.pages[0][1]
    lay.move(item.id, "page:2")
    result = view_for(monkeypatch, lay)
    diff = result["diff"]
    assert diff["has_changes"] and diff["items_moved"] == 0
    row, = diff["placement_changes"]
    assert (row["kind"], row["id"], row["from"]["page"], row["to"]["page"]) == (kind, item.id, 1, 2)
    assert view_for(monkeypatch, Layout.from_snapshot(lay.snapshot()))["diff"]["has_changes"]
    lay = lay.original()
    lay.pin(item.id, 1, 8)
    row, = view_for(monkeypatch, lay)["diff"]["placement_changes"]
    assert row["from"]["row"] != row["to"]["row"] and row["to"]["row"] == 3
    assert not view_for(monkeypatch, lay.original())["diff"]["has_changes"]


def test_web_diff_clean_normalized_folders_typed_fields_and_bank(monkeypatch):
    from homescreen.model import Layout
    from fake_phone import app
    lay = Layout([[], [{"listType":"folder","iconLists":[[],[app("child","Child")]]}, app("a","A")]], library=[])
    assert not view_for(monkeypatch, lay)["diff"]["has_changes"]
    node = lay.nodes["a"]
    node.raw["someFutureKey"]["nested"][0] = True
    assert view_for(monkeypatch, lay)["diff"]["has_changes"]
    lay = lay.original()
    lay.move("a", "bank")
    assert view_for(monkeypatch, lay)["diff"]["has_changes"]


def test_large_clean_view_sends_only_used_locations(monkeypatch):
    from homescreen.model import Layout
    from fake_phone import app
    apps = [app(f"app{i}", f"Name {i}") for i in range(512)]
    lay = Layout([[], *[apps[i:i+24] for i in range(0,288,24)]], library=apps[288:])
    assert view_for(monkeypatch, lay)["location_labels"] == {}
    lay.move("app0", "library")
    view = view_for(monkeypatch,lay,{"ok":True,"days":1,"opens":{f"app{i}":i for i in range(25)}})
    assert len(view["usage"]["top"]) == 12 and len(view["location_labels"]) <= 14


def test_folder_lifecycle_and_duplicate_bundle_usage_in_web_view(monkeypatch):
    from homescreen.model import Layout
    from fake_phone import app, folder
    lay = Layout([[], [folder("Existing", [app("dup", "First")]), app("a", "A")],
                  [app("dup", "Second")]], library=[])
    fid = lay.pages[0][0].id
    lay.rename_folder(fid, "New name")
    renamed, = view_for(monkeypatch, lay)["diff"]["placement_changes"]
    assert (renamed["reason"], renamed["before_name"], renamed["name"]) == ("renamed", "Existing", "New name")
    lay.dissolve_folder(fid)
    added = lay.create_folder("New folder", ["a"], "page:2")
    view = view_for(monkeypatch, lay, {"ok": True, "days": 2, "opens": {"dup": 10}})
    rows = {row["id"]: row for row in view["diff"]["placement_changes"]}
    assert rows[fid]["reason"] == "removed" and rows[fid]["to"] is None
    assert rows[added]["reason"] == "added" and rows[added]["from"] is None
    top, = view["usage"]["top"]
    assert top["name"] == "First" and top["where"] == lay.positions()["dup"]


def test_detached_confirm_button_cannot_dismiss_a_new_review():
    browser('''
fixture.read_only=false;vm.runInContext('render()',context);
let writes=0;context.fetch=async(path,opt)=>{assert.equal(path,'/api/apply');assert.equal(opt.method,'POST');
 writes++;return {ok:true,json:async()=>({view:fixture,result:{ok:true}})};};
$('#review').click();const previous=$('#modal-actions').lastChild;
$('#modal-actions').firstChild.click();await tick();$('#review').click();previous.click();await tick();
assert.equal(writes,0);assert.equal($('#modal').hidden,false);
$('#modal-actions').lastChild.click();await tick();assert.equal(writes,1);
''',native=True)


def test_verification_details_and_backup_in_webclip_result():
    browser('''
fixture.pages[0][0].pages[0][0].kind='webclip';
vm.runInContext("showResult({ok:false,status:'unknown',hidden_webclips:['My clip'],mismatches:{added_by_ios:['synthetic.app']},backup:'raw.plist',error:'raw diagnostic'})",context);
assert.ok($('#modal-body').textContent.includes('My clip'));assert.ok($('#modal-body').textContent.includes('raw.plist'));
assert.equal($('#modal-body').querySelector('details pre').textContent,'raw diagnostic');
$('#modal-actions').firstChild.click();
vm.runInContext("showResult({hidden_webclips:['New clip'],mismatches:{added_by_ios:['new']},backup:'recovery'})",context);
assert.ok($('#modal-body').textContent.includes('New clip'));assert.equal($('#modal-actions').children.length,1);
''',native=True)


def test_read_replacement_and_device_switch_clear_reused_ids_before_render():
    browser('''
const select=()=>{$('#pages').querySelector('[data-id="synthetic.folder"]').click();
 $('#folder-pages').querySelector('[data-id="synthetic.app"]').click();};
const clean=()=>{assert.equal($('#selection-bar').hidden,true);assert.equal($('#drawer').hidden,true);
 assert.ok(!$('#pages').querySelector('[data-id="synthetic.folder"]').classList.contains('selected'));};
context.fetch=async(path,opts)=>{assert.ok(['/api/read','/api/device'].includes(path));assert.equal(opts.method,'POST');
 return {ok:true,json:async()=>({...fixture,device_id:'reused'})};};
select();$('#read').click();await tick();assert.equal($('#modal').hidden,false);
$('#modal-actions').lastChild.click();await tick();await tick();clean();
select();await vm.runInContext("selectDevice('reused',true)",context);clean();
''',native=True)


def test_current_review_token_and_invalid_draft_actions():
    browser('''
fixture.read_only=false;fixture.diff.items_moved=0;vm.runInContext('render()',context);
assert.equal($('#review').disabled,false);assert.equal($('#summary').textContent,i18n.t('editor.layoutChanged'));
$('#review').click();fixture.diff.apply_token='changed';$('#modal-actions').lastChild.click();await tick();
assert.ok(!calls.includes('/api/apply'));
fixture.diff.problems=['bank'];$('#review').click();assert.equal($('#modal-actions').children.length,1);$('#modal-actions').firstChild.click();
fixture.diff.has_changes=false;vm.runInContext('render()',context);assert.equal($('#review').disabled,true);
''',native=True)

def test_cached_bootstrap_catalog_precedes_onboarding_script():
    browser('''
const terms=$('#consent-text');terms.scrollTop=800;terms.dispatchEvent({type:'scroll'});
$('#consent-agree').checked=true;$('#consent-agree').dispatchEvent({type:'change'});
$('#consent-continue').click();await tick();await tick();
assert.equal(context.ScreenOrganizerOnboarding.accepted,true);
assert.deepEqual(calls.filter(path=>path.startsWith('/api/')),['/api/state','/api/devices']);
''',setup="context.waitForInitialCatalog=true;")


def test_winning_host_choice_can_finish_startup_before_older_catalog():
    browser('''
assert.equal(i18n.locale,'de');
''',setup='''
stored.set('screenOrganizer.language','fr');const fetch=context.fetch;
context.fetch=p=>p.endsWith('/fr.json')?new Promise(r=>context.release=()=>fetch(p).then(r)):fetch(p);
''',before_ready='''
await tick();assert.ok(context.release);
await context.ScreenOrganizerI18n.setLocale('de');
let ready=false;context.ScreenOrganizerI18n.ready.then(()=>ready=true);await tick();
assert.ok(ready,'a stale catalog must not prevent the winning selection from completing startup');
context.release();await tick();assert.equal(context.ScreenOrganizerI18n.locale,'de');
''')


@pytest.mark.parametrize("file", ["manifest.json", "en.json"])
def test_bootstrap_catalog_failure_blocks_consent_and_device_requests(file):
    browser('''
assert.ok(context.catalogFailure);
$('#consent-continue').onclick();await tick();
assert.equal(context.ScreenOrganizerOnboarding.accepted,false);
assert.ok(calls.every(path=>!path.startsWith('/api/')));
''', setup='''
const read=context.fetch;context.fetch=(path,opts)=>path==='/static/locales/'+FILE?Promise.resolve({ok:false}):read(path,opts);
'''.replace('FILE',json.dumps(file)), before_ready="context.expectCatalogFailure=true;")


def test_app_pins_and_drop_repaint_selection_atomically():
    browser('''
$('#pages').querySelector('[data-id="synthetic.folder"]').click();$('#folder-pages').querySelector('[data-id="synthetic.app"]').click();
vm.runInContext("drag={ids:['synthetic.app']}",context);let good=false;
context.fetch=async(path,opts)=>{assert.equal(path,'/api/op');assert.equal(opts.method,'POST');
 assert.equal(JSON.parse(opts.body).op,'pin');return {ok:good,json:async()=>good?fixture:{error:'synthetic failure'}};};
const drop=()=>$('#pages').querySelector('.slot').dispatchEvent({type:'drop'});
drop();await tick();assert.ok(!$('#selection-bar').hidden);good=true;drop();await tick();
assert.equal($('#selection-bar').hidden,true);assert.ok(!$('#folder-pages').querySelector('[data-id="synthetic.app"]').classList.contains('selected'));
''',native=True)


def test_new_request_returning_to_displayed_locale_settles_consent_immediately():
    browser('''
const terms=$('#consent-text'), agree=$('#consent-agree'), select=$('#consent-language-select');
let release;const read=context.fetch;
context.fetch=(p,opt)=>p.endsWith('/fr.json')?new Promise(r=>release=()=>read(p,opt).then(r)):read(p,opt);
const first=i18n.setLocale('fr');await i18n.setLocale('system');
terms.scrollTop=800;terms.dispatchEvent({type:'scroll'});agree.checked=true;agree.onchange();
assert.equal($('#consent-continue').disabled,false);
release();await first;assert.equal(select.value,'system');assert.equal(document.documentElement.lang,'en');
assert.equal(agree.checked,true);assert.equal($('#consent-continue').disabled,false);
''')


def test_generated_backup_timestamp_and_operations_preserve_opaque_names():
    browser('''
const label=s=>vm.runInContext('backupLabel('+JSON.stringify(s)+')',context), id='f'.repeat(32);
for(const n of ['20260230-110000-before-apply-','20261113-246000-before-restore-','20261113-130000-user-defined-'])
 assert.equal(label(n+id+'.plist'),n+id+'.plist');
for(const [op,key] of [['apply','editor.beforeApply'],['restore','editor.beforeRestore']]){
 const display=label('20260228-110000-before-'+op+'-'+id+'.plist');
 assert.ok(display.includes(i18n.t(key)));assert.ok(display.includes(id));}
''',native=True)


@pytest.mark.parametrize("old_first", [True, False])
@pytest.mark.parametrize("capability", [None, "native-origin"])
def test_system_picker_notifies_host_after_os_retarget(old_first, capability):
    browser('''
const select=$('#language-select'), releases={};const fetch=context.fetch;
context.fetch=(url,opts)=>['/static/locales/fr.json','/static/locales/es.json'].includes(url)
 ?new Promise(r=>releases[url]=()=>fetch(url,opts).then(r)):fetch(url,opts);
context.navigator.languages=['fr'];select.value='system';const old=select.onchange({target:select});
context.navigator.languages=['es'];context.dispatchEvent({type:'languagechange'});await tick();
if(OLD_FIRST){releases['/static/locales/fr.json']();await old;assert.equal(bridge.length,0);}
releases['/static/locales/es.json']();await tick();assert.equal(i18n.locale,'es');assert.equal(i18n.preference,'system');
if(!OLD_FIRST){releases['/static/locales/fr.json']();await old;}
assert.deepEqual(bridge.map(m=>m.preference),['system']);assert.equal(stored.get('screenOrganizer.language'),'system');
await i18n.setLocale('system');context.navigator.languages=['en'];context.dispatchEvent({type:'languagechange'});
await tick();assert.equal(i18n.locale,'en');assert.equal(bridge.length,1);
'''.replace('OLD_FIRST',json.dumps(old_first)),native=True,
            setup=f'context.screenOrganizerHost.languageSync={json.dumps(capability)};')


def test_old_host_echo_preserves_new_picker_in_progress():
    browser('''
const select=$('#language-select');select.value='fr';await select.onchange({target:select});
let release;const fetch=context.fetch;context.fetch=(url,opts)=>url==='/static/locales/es.json'
 ?new Promise(r=>release=()=>fetch(url,opts).then(r)):fetch(url,opts);
select.value='es';const next=select.onchange({target:select});
await i18n.setLocale('fr');release();await next;assert.equal(i18n.locale,'es');
assert.deepEqual(bridge.map(m=>m.preference),['fr','es']);
await i18n.setLocale('es');await i18n.setLocale('fr');assert.equal(i18n.locale,'fr');
''',native=True)


def test_detached_name_input_never_confirms_another_dialog():
    browser('''
fixture.read_only=false;vm.runInContext('render();promptText("Folder",null,"Typed")',context);
const old=$('#modal-body input');$('#modal-actions').firstChild.click();await tick();$('#review').click();
let writes=0;context.fetch=async(path,opts)=>{assert.equal(path,'/api/apply');assert.equal(opts.method,'POST');writes++;
 return {ok:true,json:async()=>({view:fixture,result:{ok:true}})};};
for(const isComposing of [true,false])old.dispatchEvent({type:'keydown',key:'Enter',isComposing});
await tick();assert.equal(writes,0);assert.equal($('#modal').hidden,false);
''',native=True)


@pytest.mark.parametrize("replace_queued", [False, True])
def test_focused_diagnostic_retains_newest_error_until_dismissed(replace_queued):
    browser('''
const show=text=>vm.runInContext('showError(new Error('+JSON.stringify(text)+'))',context);
show('first raw');const first=$('#toast details');first.open=true;first.querySelector('summary').focus();
show('second raw');first.open=false;$('#read').focus();if(REPLACE_QUEUED)show('third raw');
vm.runInContext('toast("Later success","ok")',context);
assert.equal($('#operation-status').textContent,'Later success');assert.equal($('#toast details'),first);
$('#toast button').click();assert.ok(!$('#toast').hidden);
assert.equal($('#toast pre').textContent,REPLACE_QUEUED?'third raw':'second raw');
assert.equal(document.activeElement,$('#toast summary'));assert.equal($('#operation-status').textContent,'Later success');
$('#toast button').click();assert.ok($('#toast').hidden);assert.equal(document.activeElement,$('#read'));
'''.replace('REPLACE_QUEUED',json.dumps(replace_queued)),native=True)


@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_verified_write_warning_keeps_status_and_recovery_details(operation):
    browser('''
fixture.read_only=false;vm.runInContext('render()',context);const raw='Write returned synthetic. Readback result: matched.';
const name='recovery.plist';context.fetch=async(path,opts)=>{
 if(path==='/api/backups'&&!opts.method)return {ok:true,json:async()=>({binding:fixture.binding,backups:[name]})};
 if(path==='/api/backup-review'&&opts.method==='POST')return {ok:true,json:async()=>({review:'chosen'})};
 assert.equal(path,'/api/'+OP);assert.equal(opts.method,'POST');
 assert.equal(JSON.parse(opts.body).binding.revision,'current');
 if(OP==='restore')assert.equal(JSON.parse(opts.body).review,'chosen');
 return {ok:true,json:async()=>({view:fixture,result:{ok:true,status:'verified',error:raw,backup:name}})};};
if(OP==='restore'){$('#backups-btn').click();await tick();$('#modal-body button').click();await tick();}
else $('#review').click();
$('#modal-actions').lastChild.click();await tick();await tick();
assert.ok($('#operation-status').textContent.startsWith(OP==='apply'?i18n.t('editor.applied',{device:'iPhone'}):i18n.t('editor.restored')));
assert.ok($('#operation-status').textContent.includes(name));
assert.ok($('#toast details').textContent.includes(raw));assert.ok($('#toast').textContent.includes(name));
'''.replace('OP',json.dumps(operation)),native=True)


def test_clip_result_requires_read_even_for_known_clip():
    browser('''
await i18n.setLocale('en');fixture.pages[0][0].pages[0][0].kind='webclip';
vm.runInContext("showResult({hidden_webclips:['Known clip'],mismatches:{added_by_ios:['synthetic.app'],moved:{a:'changed'}}})",context);
assert.ok($('#modal-body p').textContent.includes('Read'));assert.ok(!$('#modal-body').textContent.includes('Everything else landed as planned'));
assert.ok($('#modal-body').textContent.includes('changed'));assert.equal($('#modal-actions').children.length,1);
''',native=True)


@pytest.mark.parametrize("kind", ["widget", "folder"])
def test_flowing_placement_cells_appear_when_neighbor_is_pinned(monkeypatch, kind):
    from homescreen.model import Layout
    from fake_phone import app, folder, widget
    raw = widget('Weather','small') if kind == 'widget' else folder('Folder',[app('c','C')])
    lay = Layout([[],[raw,app('a','A')],[app('b','B')]],library=[])
    special = lay.pages[0][0]
    lay.pin('a',1,0)
    rows = {r['id']:r for r in view_for(monkeypatch,lay)['diff']['placement_changes']}
    assert set(rows) == {special.id, 'a'}
    row = rows[special.id]
    assert row['from']['index'] == row['to']['index'] == 1
    assert (row['from']['row'], row['from']['column']) == (1,1)
    assert (row['to']['row'], row['to']['column']) == (1,3 if kind == 'widget' else 2)


def test_opaque_plist_real_bits_determine_dirtiness(monkeypatch):
    import plistlib
    import struct
    from homescreen.model import Layout
    from fake_phone import app
    def real(bits):
        return struct.unpack('>d', bytes.fromhex(bits))[0]

    for before, after in [('7ff8000000000001','7ff8000000000002'), ('8000000000000000','0000000000000000')]:
        raw=app('id','App')
        raw['opaque']={'nested':[real(before)]}
        lay=Layout(plistlib.loads(plistlib.dumps([[],[raw]],fmt=plistlib.FMT_BINARY)))
        assert not view_for(monkeypatch,lay)['diff']['has_changes']
        lay.nodes['id'].raw['opaque']['nested'][0]=real(after)
        assert view_for(monkeypatch,lay)['diff']['has_changes']


def test_usage_ranking_avoids_full_positions_in_web_view(monkeypatch):
    from homescreen import usage
    from homescreen.model import Layout
    from fake_phone import app
    apps=[app(f'a{i}',str(i)) for i in range(512)]
    lay=Layout([[],apps[:24]],library=apps[24:])
    calls=[]
    positions=Layout.positions
    def counted(layout, *args):
        calls.append(layout)
        return positions(layout, *args)
    monkeypatch.setattr(Layout,'positions',counted)
    view_for(monkeypatch,lay)
    baseline=len(calls)
    for opens in ({}, {'a2':2},{f'a{i}':20-i for i in range(20)}):
        calls.clear()
        result=view_for(monkeypatch,lay,{'ok':True,'days':1,'opens':opens})
        assert len(calls)==baseline
        assert result['usage']['top']==usage.top_apps(lay,opens,12)
        assert len(result['location_labels'])==min(len(opens),12)


@pytest.mark.parametrize("suffix", ["doesn't fit at row 6, column 1", "can't be pinned at row 1, column 6"])
def test_named_position_diagnostic_precedes_usage_and_backup_prefixes(suffix):
    browser('''
for(const name of ['Clock','backup clock','iOS 16 clock','Read-only mode',"the phone's clock",'No synced Screen Time data on this Mac.']){
 const raw=name+' '+SUFFIX;
 vm.runInContext('showError(new Error('+JSON.stringify(raw)+'))',context);
 assert.equal($('#operation-status').textContent,i18n.t('error.position'),name);
 assert.equal($('#toast pre').textContent,raw);
}
'''.replace('SUFFIX',json.dumps(suffix)),native=True)


def test_native_may_coalesce_picker_acknowledgments():
    browser('''
const select=$('#language-select');
for(const code of ['fr','es']){select.value=code;await select.onchange({target:select});}
await i18n.setLocale('es');
await i18n.setLocale('fr');
assert.equal(i18n.locale,'fr');
assert.deepEqual(bridge.map(m=>m.preference),['fr','es']);
''',native=True)


@pytest.mark.parametrize("source,expected", [("picker", "pt"), ("host", "de")])
def test_new_explicit_choice_supersedes_system_notification(source,expected):
    browser('''
const select=$('#language-select');let release;const fetch=context.fetch;
context.fetch=(url,opts)=>url==='/static/locales/fr.json'?new Promise(r=>release=()=>fetch(url,opts).then(r)):fetch(url,opts);
context.navigator.languages=['fr'];select.value='system';const old=select.onchange({target:select});
if(SOURCE==='picker'){select.value=EXPECTED;await select.onchange({target:select});}
else await i18n.setLocale(EXPECTED);
release();await old;assert.equal(i18n.locale,EXPECTED);assert.equal(i18n.preference,EXPECTED);
assert.deepEqual(bridge.map(m=>m.preference),SOURCE==='picker'?[EXPECTED]:[]);
'''.replace('SOURCE',json.dumps(source)).replace('EXPECTED',json.dumps(expected)),native=True)


def test_native_origin_host_keeps_first_real_selection_after_coalescing():
    browser('''
const select=$('#language-select');
for(const code of ['fr','de']){select.value=code;await select.onchange({target:select});}
assert.deepEqual(bridge.map(m=>m.preference),['fr','de']);
await i18n.setLocale('fr');
assert.equal(i18n.preference,'fr');assert.equal(i18n.locale,'fr');assert.equal(select.value,'fr');
assert.equal(stored.get('screenOrganizer.language'),'fr');assert.equal(bridge.length,2);
''',native=True,setup="context.screenOrganizerHost.languageSync='native-origin';")


@pytest.mark.parametrize("capability", [None, "native-origin"])
def test_explicit_native_request_supersedes_picker_and_retires_legacy_acknowledgment(capability):
    browser('''
const select=$('#language-select');select.value='fr';await select.onchange({target:select});
const fetch=context.fetch;let release;context.fetch=(p,opts)=>p==='/static/locales/es.json'
 ?new Promise(r=>release=()=>fetch(p,opts).then(r)):fetch(p,opts);
select.value='es';const pending=select.onchange({target:select});
await i18n.setLocale('pt','native');release();await pending;assert.equal(select.value,'pt');
await i18n.setLocale('fr');assert.equal(select.value,'fr');assert.deepEqual(bridge.map(m=>m.preference),['fr']);
''',native=True,setup=f'context.screenOrganizerHost.languageSync={json.dumps(capability)};')
