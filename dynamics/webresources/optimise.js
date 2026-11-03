// "Optimise with Timefold": opens the run dialog from the Field Service app's
// command bar. Registered as web resource tf_/optimise.js; the button is a
// modern command (appaction) calling Timefold.openOptimiseDialog.
var Timefold = window.Timefold || {};
Timefold.openOptimiseDialog = function () {
    return Xrm.Navigation.navigateTo(
        { pageType: "webresource", webresourceName: "tf_/optimise.html" },
        { target: 2, position: 1, width: { value: 560, unit: "px" },
          height: { value: 640, unit: "px" }, title: "Optimise with Timefold" });
};
window.Timefold = Timefold;
