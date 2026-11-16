const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const { installedUpdater } = require('../installed-updates');

function runtime(t, options = {}) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ooc-installed-'));
    t.after(() => fs.rmSync(root, {recursive:true,force:true}));
    const record = {schema:1,product:'object-oriented-circuits',version:'1.1.0',python:'versions/1.1.0/python.exe',
        library:'versions/1.1.0/chips',extension:'versions/1.1.0/ooc.vsix',runtime_bundle:'versions/1.1.0/runtime.json'};
    for (const key of ['python','library','extension','runtime_bundle']) {
        const file = path.join(root,record[key]); fs.mkdirSync(path.dirname(file),{recursive:true});
        if (key==='library') fs.mkdirSync(file); else fs.writeFileSync(file,'fixture');
    }
    fs.writeFileSync(path.join(root,record.library,'demo.ckt'),'chip demo() {}');
    const save = () => fs.writeFileSync(path.join(root,'install.json'),JSON.stringify(record)); save();
    const project = path.join(root,'user project');fs.mkdirSync(project);
    const doc = {uri:{scheme:'file',fsPath:path.join(project,'nested','test.ckt')}};
    const vscode = {SymbolKind:{},workspace:{
        getConfiguration:()=>({get:(key,fallback)=>options[key]??fallback}),
        getWorkspaceFolder:()=>({uri:{fsPath:project}}),
    }};
    const module={exports:{}};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../runtime.js'),'utf8'),{
        module,process:{platform:'win32',env:{OOC_INSTALL_ROOT:root}},
        require:(name)=>name==='vscode'?vscode:name==='./structure'?require('../structure'):
            name==='./hover-client'?require('../hover-client'):require(name),
    });
    return {api:module.exports,root,project,doc,record,save};
}

test('installed runtime, chips and workspace identity work without a source checkout', t=>{
    const f=runtime(t);
    assert.equal(f.api.checkoutRoot(f.doc),undefined);
    assert.equal(f.api.runtime(f.doc).python,path.join(f.root,f.record.python));
    assert.equal(f.api.runtime(f.doc).cwd,f.project);
    assert.equal(f.api.includeTarget(f.doc,'@chips/demo.ckt'),path.join(f.root,f.record.library,'demo.ckt'));
    assert.equal(f.api.includeTarget(f.doc,'@chips/../../escape.ckt'),undefined);
});

test('explicit interpreter wins and malformed installed paths are ignored',t=>{
    const f=runtime(t,{pythonPath:'chosen-python'});
    assert.equal(f.api.runtime(f.doc).python,'chosen-python');
    f.record.python='../escape.exe';f.save();
    assert.equal(f.api.installedRuntime(),undefined);
    f.record.product='other';f.save();
    assert.equal(f.api.installedRuntime(),undefined);
});

test('short chip names navigate in an installed library without changing local paths',t=>{
    const f=runtime(t);
    const library=path.join(f.root,f.record.library);
    fs.mkdirSync(path.join(library,'logic'));
    const chip=path.join(library,'logic','74HC00.ckt');
    fs.writeFileSync(chip,'include("SN74HC00.ckt");');
    assert.equal(f.api.includeTarget(f.doc,'74hc00'),chip);
    assert.equal(f.api.includeTarget(f.doc,'@chips/logic/74HC00.ckt'),chip);
    assert.equal(f.api.includeTarget(f.doc,'74HC00.ckt'),path.join(f.project,'nested','74HC00.ckt'));
    assert.equal(f.api.includeTarget(f.doc,'MissingChip'),undefined);
    assert.equal(f.api.includeTarget(f.doc,'@chips/../../outside.ckt'),undefined);
    fs.mkdirSync(path.join(library,'duplicate'));
    fs.writeFileSync(path.join(library,'duplicate','74HC00.ckt'),'chip duplicate() {}');
    assert.equal(f.api.includeTarget(f.doc,'74HC00'),undefined);
    assert.equal(f.api.includeTarget(f.doc,'@chips/logic/74HC00.ckt'),chip);
});

test('short chip navigation uses the configured checkout, including untitled documents',t=>{
    const root=path.resolve(__dirname,'../../..');
    const f=runtime(t,{projectRoot:root});
    const chip=path.join(root,'circuitlab','libraries','chips','logic','74HC00.ckt');
    assert.equal(f.api.includeTarget(f.doc,'74HC00'),chip);
    assert.equal(f.api.includeTarget({uri:{scheme:'untitled',fsPath:'Untitled-1'}},'74HC00'),chip);
    assert.equal(f.api.includeTarget({uri:{scheme:'untitled',fsPath:'Untitled-1'}},'local.ckt'),undefined);
});

function host(t,{trusted=true,enabled=true,choice='Later',fail=false}={}) {
    const calls=[],messages=[],commands=[],logs=[];
    let descriptor={root:'installed',python:'python',version:'1.1.0',extensionVersion:'0.7.0'};
    const context={extension:{packageJSON:{version:'0.7.0'}},subscriptions:[]};
    const vscode={workspace:{isTrusted:trusted,getConfiguration:()=>({get:()=>enabled})},
        window:{showInformationMessage:async(msg)=>{messages.push(msg);return choice;},
            showErrorMessage:msg=>messages.push(msg),withProgress:async(_,task)=>task()},
        commands:{executeCommand:async(...args)=>commands.push(args)},ProgressLocation:{Notification:1}};
    const check=installedUpdater(vscode,context,()=>descriptor,{appendLine:s=>logs.push(s)},async()=>true,
        async(install,action,version)=>{calls.push({action,version});if(fail)throw Error('broken download');
            if(action==='check')return {available:true,release:{version:'1.2.0'}};
            descriptor={...descriptor,version};return {ok:true};});
    t.after(()=>context.subscriptions.forEach(s=>s.dispose()));
    return {check,calls,messages,commands,logs};
}

test('installed updates check automatically, throttle, and wait for Install update',async t=>{
    const h=host(t);await h.check();await h.check();
    assert.equal(h.calls.length,1);assert.equal(h.calls[0].action,'check');
    assert.equal(h.commands.length,0);
    const accepted=host(t,{choice:'Install update'});await accepted.check(true);
    assert.deepEqual(accepted.calls.map(c=>c.action),['check','apply']);
    assert.match(accepted.messages.at(-1),/installed/);
});

test('trust, disabled checking, and failed downloads prevent successful updates',async t=>{
    const restricted=host(t,{trusted:false});await restricted.check(true);assert.equal(restricted.calls.length,0);
    const disabled=host(t,{enabled:false});await disabled.check();assert.equal(disabled.calls.length,0);
    await disabled.check(true);assert.equal(disabled.calls.length,1);
    const failed=host(t,{fail:true});await failed.check(true);
    assert.match(failed.messages.at(-1),/failed/);assert.equal(failed.commands.length,0);
});
