// "Optimize with Timefold": opens the run dialog from the Field Service app's
// command bar. Registered as web resource tf_/optimize.js; the button is a
// modern command (appaction) calling Timefold.openOptimizeDialog.
var Timefold = window.Timefold || {};
Timefold.openOptimizeDialog = function () {
    return Xrm.Navigation.navigateTo(
        { pageType: "webresource", webresourceName: "tf_/optimize.html" },
        { target: 2, position: 1, width: { value: 560, unit: "px" },
          height: { value: 640, unit: "px" }, title: "Optimize with Timefold" });
};
window.Timefold = Timefold;
