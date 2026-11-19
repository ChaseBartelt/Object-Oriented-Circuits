% Run the original MATLAB implementation to create an independent historical regression oracle.
% Solve and render errors are captured separately, and complex values are serialized explicitly.
function captureReference(legacyRoot, casePath, resultPath)
% Capture resolved topology and numerical results from the original MATLAB app.
addpath(legacyRoot);
cases = jsondecode(fileread(casePath));
template = struct('id','','mode','','error','','renderError','','known',[],'calculated',[], ...
    'diagnostics',{{}},'branches',[],'renderBranches',[],'network',[],'dcNetwork',[], ...
    'positions',[],'paths',[],'probeValues',[],'legacyMeasurement',[], ...
    'equations',[],'rowLabels',{{}});
results = repmat(template,1,numel(cases));
% Keep one result record for every requested case, including cases that intentionally fail.
for i = 1:numel(cases)
    item = cases(i);
    out = template;
    out.id = item.id;
    mode = item.mode;
    source = item.source;
    if strcmp(mode,'auto'), mode = ec.analysisMode(source); end
    out.mode = mode;
    try
        result = ec.solveInput(mode,source);
        out.mode = result.context;
        out.known = packValues(result.known);
        out.calculated = packValues(result.calculated);
        out.diagnostics = result.diagnostics;
        out.equations = result.equations;
        rows = ec.ui.solverRows(result);
        out.rowLabels = rows(:,2);
        out.branches = packBranches(result.elements);
        out.network = packNetwork(result.network);
        out.dcNetwork = packNetwork(result.dcNetwork);
        if ~isempty(result.probeResults)
            out.probeValues = packArray([result.probeResults.value]);
        end
        if ~isempty(result.measurement)
            out.legacyMeasurement = result.measurement;
        end
    catch failure
        out.error = failure.identifier;
    end
    % Capture geometry independently of numeric success; a circuit may render while its solve fails.
    if item.render
        try
            rendering = ec.solveInput(mode,source,true);
            out.renderBranches = packBranches(rendering.elements);
            labels = cellfun(@(name) {name}, {rendering.elements.name}, 'UniformOutput', false);
            routes = ec.ui.routeSchematic(rendering.elements,labels);
            out.positions = routes.positions;
            out.paths = struct('net',routes.nets,'segments',routes.paths, ...
                'junctions',routes.junctions);
        catch failure
            out.renderError = failure.identifier;
        end
    end
    results(i) = out;
end
% Write the complete reference data only after all cases have been collected.
file = fopen(resultPath,'w','n','UTF-8');
cleanup = onCleanup(@() fclose(file));
fprintf(file,'%s\n',jsonencode(results,PrettyPrint=true));
fprintf('REFERENCE CAPTURED: %d cases\n',numel(results));
end

% JSON has no complex scalar type, so retain real and imaginary parts as a pair.
function values = packValues(raw)
values = struct();
for name = string(fieldnames(raw)).'
    v = raw.(name);
    values.(name) = [real(v),imag(v)];
end
end

% Flatten values consistently and keep a separate real/imaginary column for each entry.
function array = packArray(values)
array = [real(values(:)),imag(values(:))];
end

% Preserve component ordering and terminal names so topology comparisons remain independent
% of how later Python code chooses to implement its solver.
function parts = packBranches(elements)
parts = repmat(struct('name','','kind','','p','','n','','plus','','minus','','value',[]),1,numel(elements));
for i = 1:numel(elements)
    e = elements(i);
    parts(i) = struct('name',e.name,'kind',e.type,'p',e.p,'n',e.n, ...
        'plus',e.inPlus,'minus',e.inMinus,'value',packArray(e.value));
end
end

% Capture solved node/branch arrays and optional port equivalents using the same pair encoding.
function out = packNetwork(network)
out = [];
if isempty(network), return; end
out = struct('nodes',{network.nodes},'names',{network.names}, ...
    'voltage',packArray(network.voltage),'current',packArray(network.branchCurrent), ...
    'branchVoltage',packArray(network.branchVoltage),'power',packArray(network.branchPower), ...
    'frequency',network.frequency,'equivalent',[]);
if ~isempty(network.equivalent)
    e = network.equivalent;
    out.equivalent = struct('voltage',packArray(e.openCircuitVoltage), ...
        'impedance',packArray(e.impedance),'current',packArray(e.shortCircuitCurrent));
end
end
